from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

from playwright.sync_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

STATE_FILE = Path("data/storage_state.json")
TARGET_URL = "https://tp.educloud.no/ntnu/app/rombestilling/"
LOGIN_TIMEOUT_MS = 600_000  # 10 minutes for MFA push


class LoginTimeoutError(RuntimeError):
    """Raised when MFA approval was not completed within the timeout window."""

# Type for the notify callback: (number_str, screenshot_bytes) -> None
NotifyFn = Callable[[str, bytes], None]


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


def _is_login_page(page: Page) -> bool:
    h = _host(page.url)
    return h in ("idp.feide.no", "auth.dataporten.no", "login.microsoftonline.com", "login.microsoft.com")


def _is_authenticated(page: Page) -> bool:
    url = page.url
    h = _host(url)
    return h == "tp.educloud.no" and "login" not in url and not _is_login_page(page)


def _submit_feide_org_selector(page: Page) -> None:
    """Submit Feide org-selector to pick NTNU (auth.dataporten.no)."""
    page.evaluate("""() => {
        const inp = document.getElementById('org_selector');
        if (inp) inp.value = 'feide|realm|ntnu.no';
        const form = document.getElementById('discoform_feide');
        if (form) form.submit();
    }""")


def _get_feide_password(user: str) -> str:
    """Read password from file (FEIDE_PASSWORD_FILE), keyring, or FEIDE_PASSWORD env var."""
    pwd_file = os.environ.get("FEIDE_PASSWORD_FILE", "")
    if pwd_file:
        try:
            return Path(pwd_file).read_text().strip()
        except Exception:
            pass
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
    """Click the first link/button whose text contains any of the given strings.

    Uses Playwright's real mouse simulation (isTrusted=true) so pages that
    check event.isTrusted won't silently ignore the click."""
    for text in texts:
        # Try interactive elements first (prefer anchor/button; use force so hidden elements work)
        for sel in (
            f"a:has-text('{text}')",
            f"button:has-text('{text}')",
            f"[role='button']:has-text('{text}')",
        ):
            try:
                loc = page.locator(sel)
                n = loc.count()
                if n > 0:
                    logger.debug("_try_click: found %d '%s' via %s", n, text, sel)
                    loc.first.click(timeout=2000, force=True)
                    logger.debug("_try_click: clicked '%s'", text)
                    return True
            except Exception as e:
                logger.debug("_try_click: %s / '%s' raised: %s", sel, text, e)
        # Fallback: any element containing the text
        try:
            loc = page.get_by_text(text, exact=False)
            n = loc.count()
            if n > 0:
                logger.debug("_try_click: get_by_text found %d '%s'", n, text)
                loc.first.click(timeout=2000, force=True)
                logger.debug("_try_click: force-clicked '%s'", text)
                return True
        except Exception as e:
            logger.debug("_try_click: get_by_text '%s' raised: %s", text, e)
    logger.debug("_try_click: no element found for any of %s", texts)
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
            logger.debug("wait_for_timeout interrupted - page likely closed")
            return False
        elapsed += step_ms

        try:
            url = page.url
        except Exception:
            logger.debug("page.url failed - page likely closed")
            return False

        host = _host(url)
        logger.debug("chain poll [%ds] host=%s", elapsed // 1000, host)

        if host == "tp.educloud.no" and not _is_login_page(page):
            page.wait_for_timeout(3000)
            return True

        # Feide org selector: pick NTNU
        if host == "auth.dataporten.no":
            try:
                _submit_feide_org_selector(page)
            except Exception:
                pass
            continue

        # Feide/NTNU IdP: click Microsoft method button (method-selection page),
        # or fill Feide username/password if those fields are present.
        if host == "idp.feide.no":
            # Method selection: each provider is an <article> or <form>.
            # The Microsoft link text is "Logg inn med Microsoft" (NO) inside P.hidden
            # inside a <form>. Submit the right form or click the article.
            clicked = False
            for needle in ("Logg inn med Microsoft", "Microsoft", "Use work or school account"):
                # Try article/li wrapping the method button
                for outer in (f"article:has-text('{needle}')", f"li:has-text('{needle}')"):
                    try:
                        loc = page.locator(outer)
                        if loc.count() > 0:
                            logger.debug("Clicking method container '%s' for '%s'", outer, needle)
                            loc.first.click(force=True, timeout=2000)
                            clicked = True
                            break
                    except Exception as e:
                        logger.debug("article click '%s' raised: %s", needle, e)
                if clicked:
                    break
                # Fallback: submit the form containing the text
                try:
                    result = page.evaluate(f"""() => {{
                        for (const form of document.querySelectorAll('form')) {{
                            if (form.textContent.includes('{needle}')) {{
                                form.submit();
                                return true;
                            }}
                        }}
                        return false;
                    }}""")
                    if result:
                        logger.debug("Submitted form for '%s'", needle)
                        clicked = True
                        break
                except Exception as e:
                    logger.debug("form submit '%s' raised: %s", needle, e)
            if clicked:
                continue
            # Only try credentials if actual input fields exist (Feide username/password page)
            if page.locator("input[name=username], input[type=email]").count() > 0:
                _autofill_credentials(page)
            continue

        if not (host == "login.microsoftonline.com" or host == "login.microsoft.com"):
            continue

        # Passkey dialog - dismiss and fall back to password/other method.
        # The credentials API is blocked via init script, but if we still land here
        # press Escape to abort any native browser dialog that slipped through.
        if "fido" in url:
            _try_click(page, "Cancel", "Sign in another way", "Use a different method", "Avbryt", "Try another way")
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            continue

        # Number-matching challenge: must check BEFORE "Sign in another way" because
        # the number-matching page also has a "Sign in another way" link that would
        # send us back to method selection and break the loop.
        mfa_num = _extract_mfa_number(page)
        if mfa_num:
            logger.info("MFA number: %s - enter this on your phone", mfa_num)
            if not mfa_notified and notify_fn:
                try:
                    try:
                        screenshot = page.screenshot(timeout=5000)
                    except Exception as se:
                        logger.info("Screenshot failed, sending without: %s", se)
                        screenshot = b""
                    notify_fn(mfa_num, screenshot)
                    mfa_notified = True
                    print(f"MFA number emailed: {mfa_num}")
                except Exception as e:
                    logger.info("MFA email failed (will retry): %s", e)
            continue

        # "Sign in another way" on passkey-error or post-autofill pages
        if _try_click(page, "Sign in another way"):
            continue

        # MFA method list - choose Authenticator app push
        if _try_click(
            page,
            "Approve a request on my Microsoft Authenticator app",
            "Approve a request",
        ):
            continue

        # Auto-fill email/password if fields are present
        _autofill_credentials(page)

    logger.debug("SSO chain timed out (URL: %s)", page.url)
    return False


def _block_passkeys(ctx: BrowserContext) -> None:
    """Override credentials API so passkey dialogs are rejected before they appear."""
    ctx.add_init_script("""
        Object.defineProperty(navigator, 'credentials', {
            get: () => ({
                get: () => Promise.reject(new DOMException('NotAllowedError', 'Passkey blocked by koker')),
                create: () => Promise.reject(new DOMException('NotAllowedError', 'Passkey blocked by koker')),
            }),
            configurable: true,
        });
    """)


def _try_feide_sso(page: Page) -> bool:
    """Submit NTNU on Feide org-selection page and wait for silent SSO redirect.

    Works without MFA when stored Microsoft/NTNU session cookies are still valid."""
    if _host(page.url) != "auth.dataporten.no":
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
        _block_passkeys(ctx)
        page = ctx.new_page()
        page.goto(TARGET_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)

        if _is_authenticated(page):
            print("Session restored from storage_state.json")
            return browser, ctx, page

        if _host(page.url) == "auth.dataporten.no":
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
    _block_passkeys(ctx)
    page = ctx.new_page()
    page.goto(TARGET_URL, wait_until="domcontentloaded")

    if notify_fn:
        print("Re-auth needed. Will email MFA number when ready.")
    else:
        print("\nApprove the Microsoft Authenticator push when prompted.\n")

    if not _wait_for_tp_through_feide_chain(page, timeout_ms=LOGIN_TIMEOUT_MS, notify_fn=notify_fn):
        raise LoginTimeoutError(
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
