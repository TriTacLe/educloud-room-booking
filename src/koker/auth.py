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


def _click_microsoft_method(page: Page) -> bool:
    """Click the Microsoft login method on a Feide/Dataporten method-select page.
    The button text varies by locale and host: auth.dataporten.no shows
    'Bruk arbeids- eller skolekonto', idp.feide.no shows 'Logg inn med Microsoft'."""
    # idp.feide.no renders the Microsoft option as <input type="submit">, which
    # :has-text() cannot match (label lives in the value attribute, not a text
    # node). Target it directly before falling back to text-based selectors.
    for sel in (
        "#microsoft-signin-button",
        "form[action*='login-azuread'] input[type='submit']",
    ):
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                logger.debug("Clicking Microsoft submit '%s'", sel)
                loc.first.click(force=True, timeout=2000)
                return True
        except Exception as e:
            logger.debug("microsoft submit click '%s' raised: %s", sel, e)

    for needle in (
        "Bruk arbeids- eller skolekonto",
        "Logg inn med Microsoft",
        "Use work or school account",
        "Microsoft",
    ):
        for sel in (
            f"button:has-text('{needle}')",
            f"a:has-text('{needle}')",
            f"article:has-text('{needle}')",
            f"li:has-text('{needle}')",
        ):
            try:
                loc = page.locator(sel)
                if loc.count() > 0:
                    logger.debug("Clicking method '%s' for '%s'", sel, needle)
                    loc.first.click(force=True, timeout=2000)
                    return True
            except Exception as e:
                logger.debug("method click '%s' raised: %s", sel, e)
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
                return True
        except Exception as e:
            logger.debug("form submit '%s' raised: %s", needle, e)
    return False


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
    mfa_emailed = ""

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
            # A fresh context briefly serves the TP SPA shell before its JS
            # redirects to Feide. Re-check after the settle wait so an
            # unauthenticated shell is not mistaken for a live session.
            page.wait_for_timeout(3000)
            if _host(page.url) == "tp.educloud.no" and not _is_login_page(page):
                return True
            continue

        # Feide org selector (old page) or method-select page (new page): pick
        # NTNU, then the Microsoft method button if present.
        if host == "auth.dataporten.no":
            try:
                _submit_feide_org_selector(page)
            except Exception:
                pass
            _click_microsoft_method(page)
            continue

        # Feide/NTNU IdP: click Microsoft method button (method-selection page),
        # or fill Feide username/password if those fields are present.
        if host == "idp.feide.no":
            if _click_microsoft_method(page):
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
            # Re-send when the number rotates so the mail never carries a stale number.
            if notify_fn and mfa_emailed != mfa_num:
                try:
                    try:
                        screenshot = page.screenshot(timeout=5000)
                    except Exception as se:
                        logger.info("Screenshot failed, sending without: %s", se)
                        screenshot = b""
                    notify_fn(mfa_num, screenshot)
                    mfa_emailed = mfa_num
                    logger.info("MFA number emailed: %s", mfa_num)
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
        # Let errors propagate: the chain logs them at INFO and retries on
        # the next poll instead of silently marking the number as sent.
        from koker.notifier import send_mfa_number
        send_mfa_number(number, screenshot, [to_addr], app_password, from_addr or to_addr)

    return notify


def _goto_with_retry(page: Page, url: str, attempts: int = 3) -> None:
    """Navigate with backoff. The first run after the server wakes from
    suspend often hits a dead network for a few seconds."""
    for i in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded")
            return
        except Exception as e:
            if i == attempts - 1:
                raise
            wait = 10 * (2 ** i)
            logger.info("goto %s failed (%s), retrying in %ds", url, e, wait)
            page.wait_for_timeout(wait * 1000)


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
        _goto_with_retry(page, TARGET_URL)
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
    # Keep stored cookies for the re-auth pass: ESTSAUTHPERSISTENT lets
    # Microsoft skip password + MFA even when the TP/Feide session is dead.
    if STATE_FILE.exists():
        ctx = browser.new_context(storage_state=str(STATE_FILE))
    else:
        ctx = browser.new_context()
    _block_passkeys(ctx)
    page = ctx.new_page()
    _goto_with_retry(page, TARGET_URL)

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
