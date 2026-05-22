from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Callable, Optional, Tuple

from playwright.sync_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

STATE_FILE = Path("data/storage_state.json")
TARGET_URL = "https://tp.educloud.no/ntnu/app/rombestilling/"
LOGIN_TIMEOUT_MS = 300_000  # 5 minutes for MFA push

# Type for the notify callback: (number_str, screenshot_bytes) -> None
NotifyFn = Callable[[str, bytes], None]


def _is_login_page(page: Page) -> bool:
    url = page.url
    return (
        "idp.feide.no" in url
        or "auth.dataporten.no" in url
        or "login.microsoftonline.com" in url
    )


def _is_authenticated(page: Page) -> bool:
    url = page.url
    return (
        "tp.educloud.no/ntnu" in url
        and "login" not in url
        and not _is_login_page(page)
    )


def _submit_feide_org_selector(page: Page) -> None:
    """Submit Feide org-selector to pick NTNU (auth.dataporten.no)."""
    page.evaluate("""() => {
        const inp = document.getElementById('org_selector');
        if (inp) inp.value = 'feide|realm|ntnu.no';
        const form = document.getElementById('discoform_feide');
        if (form) form.submit();
    }""")


def _get_feide_password(user: str) -> str:
    """Read password from system keyring, fall back to FEIDE_PASSWORD env var."""
    if user:
        try:
            import subprocess
            result = subprocess.run(
                ["secret-tool", "lookup", "service", "koker", "username", user],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass
    return os.environ.get("FEIDE_PASSWORD", "")


def _autofill_credentials(page: Page) -> None:
    """Fill username/email then password. Works on both Feide and Microsoft pages."""
    user = os.environ.get("FEIDE_USER", "")
    pwd = _get_feide_password(user)
    try:
        if user:
            for sel in ["input[name=username]", "input[type=email]"]:
                field = page.locator(sel)
                if field.count() > 0:
                    try:
                        field.first.wait_for(state="visible", timeout=2000)
                        if field.first.input_value() == "":
                            field.first.fill(user)
                            page.keyboard.press("Enter")
                            page.wait_for_timeout(1500)
                    except Exception:
                        pass
                    break
        if pwd:
            password = page.locator("input[type=password]")
            if password.count() > 0:
                try:
                    password.first.wait_for(state="visible", timeout=5000)
                    password.first.fill(pwd)
                    page.keyboard.press("Enter")
                except Exception:
                    pass
    except Exception:
        pass


def _try_click(page: Page, *texts: str) -> bool:
    """Click the first visible element matching any of the given texts."""
    for text in texts:
        try:
            el = page.get_by_text(text, exact=False)
            if el.count() > 0 and el.first.is_visible(timeout=1000):
                el.first.click()
                return True
        except Exception:
            pass
    return False


def _extract_mfa_number(page: Page) -> str:
    """Extract the 2-digit number from Microsoft's number-matching MFA page."""
    # Try known element IDs first
    for sel in ["#idRichContext_DisplaySign", "[data-testid*='displaySign']", ".displaySign"]:
        try:
            el = page.locator(sel)
            if el.count() > 0:
                text = el.first.text_content(timeout=1000) or ""
                text = text.strip()
                if text.isdigit():
                    return text
        except Exception:
            pass
    # Fallback: scan page body for a prominent 2-digit number
    try:
        body = page.locator("body").text_content(timeout=2000) or ""
        # Look for 2-digit numbers surrounded by whitespace (the displayed code)
        matches = re.findall(r'(?<!\d)(\d{2})(?!\d)', body)
        if matches:
            return matches[0]
    except Exception:
        pass
    return ""


def _wait_for_tp_through_feide_chain(
    page: Page,
    timeout_ms: int = 90_000,
    notify_fn: Optional[NotifyFn] = None,
) -> bool:
    """Poll until TP is reached. Auto-navigates Feide org selector, NTNU method picker,
    Microsoft login (email+password), passkey cancel, MFA method selection, and number
    matching. Calls notify_fn(number, screenshot) once when number appears."""
    step_ms = 2_000
    elapsed = 0
    mfa_notified = False

    while elapsed < timeout_ms:
        try:
            page.wait_for_timeout(step_ms)
        except Exception:
            return False  # browser or page closed
        elapsed += step_ms

        try:
            url = page.url
        except Exception:
            return False

        if "tp.educloud.no" in url and not _is_login_page(page):
            page.wait_for_timeout(3000)
            return True

        # Feide org selector: pick NTNU
        if "auth.dataporten.no" in url:
            try:
                _submit_feide_org_selector(page)
            except Exception:
                pass
            continue

        # Feide/NTNU IdP: fill Feide credentials OR click Microsoft method button
        if "idp.feide.no" in url or "feide.no/simplesaml" in url:
            _autofill_credentials(page)
            _try_click(page, "Use work or school account")
            continue

        if "login.microsoftonline.com" not in url:
            continue

        # Passkey dialog - click Cancel and fall back to password/other method
        if "fido" in url:
            _try_click(page, "Cancel", "Sign in another way", "Use a different method")
            continue

        # "Sign in another way" after passkey failure or on method selection page
        if _try_click(page, "Sign in another way"):
            continue

        # MFA method list - choose Authenticator app push
        if _try_click(
            page,
            "Approve a request on my Microsoft Authenticator app",
            "Approve a request",
        ):
            continue

        # Number-matching challenge: extract number, email it once
        if not mfa_notified and notify_fn:
            num = _extract_mfa_number(page)
            if num:
                try:
                    screenshot = page.screenshot()
                    notify_fn(num, screenshot)
                    mfa_notified = True
                    print(f"MFA number emailed: {num}")
                except Exception as e:
                    logger.debug("Failed to send MFA notification: %s", e)

        # Auto-fill email/password if fields are present
        _autofill_credentials(page)

    logger.debug("SSO chain timed out (URL: %s)", page.url)
    return False


def _try_feide_sso(page: Page) -> bool:
    """Submit NTNU on Feide org-selection page and wait for silent SSO redirect.

    Works without MFA when stored Microsoft/NTNU session cookies are still valid."""
    if "auth.dataporten.no" not in page.url:
        return False
    try:
        _submit_feide_org_selector(page)
        return _wait_for_tp_through_feide_chain(page)
    except Exception as e:
        logger.debug("Feide SSO failed: %s", e)
        return False


def _make_mfa_notifier() -> Optional[NotifyFn]:
    """Build a notify_fn that emails the MFA number + screenshot using env-var Gmail creds."""
    from_addr = os.environ.get("GMAIL_FROM", "")
    to_addr = os.environ.get("GMAIL_TO", "")
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not (to_addr and app_password):
        return None

    def notify(number: str, screenshot: bytes) -> None:
        try:
            from koker.notifier import send_mfa_number
            send_mfa_number(number, screenshot, [to_addr], app_password, from_addr or to_addr)
        except Exception as e:
            logger.debug("MFA notify failed: %s", e)

    return notify


def get_authenticated_context(p: Playwright, headless: bool = True) -> Tuple[Browser, BrowserContext, Page]:
    browser = p.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--js-flags=--max-old-space-size=512",
        ],
    )

    if STATE_FILE.exists():
        ctx = browser.new_context(storage_state=str(STATE_FILE))
        page = ctx.new_page()
        page.goto(TARGET_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        if _is_authenticated(page):
            print("Session restored from storage_state.json")
            return browser, ctx, page

        if "auth.dataporten.no" in page.url:
            logger.info("SPA redirected to Feide; trying silent SSO re-auth")
            if _try_feide_sso(page):
                print("Session restored via SSO re-auth")
                ctx.storage_state(path=str(STATE_FILE))
                return browser, ctx, page

        print("Session expired, attempting full re-auth...")
        page.close()
        ctx.close()

    notify_fn = _make_mfa_notifier()
    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(TARGET_URL, wait_until="domcontentloaded")

    if notify_fn:
        print("Re-auth needed. Will email MFA number when ready.")
    else:
        print("\nApprove the Microsoft Authenticator push when prompted.\n")

    if not _wait_for_tp_through_feide_chain(page, timeout_ms=LOGIN_TIMEOUT_MS, notify_fn=notify_fn):
        raise RuntimeError(
            "Login timed out. Make sure you approved the Authenticator push."
        )

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ctx.storage_state(path=str(STATE_FILE))
    print("Session saved to data/storage_state.json")

    return browser, ctx, page


def refresh_session(ctx: BrowserContext) -> None:
    """Re-export cookies after a successful operation so they stay fresh."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ctx.storage_state(path=str(STATE_FILE))


def check_session_live(page: Page) -> bool:
    """Return False if the page has drifted to a login flow."""
    return _is_authenticated(page)
