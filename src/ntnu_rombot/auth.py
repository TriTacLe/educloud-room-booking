from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple

from playwright.sync_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

STATE_FILE = Path("data/storage_state.json")
TARGET_URL = "https://tp.educloud.no/ntnu/app/rombestilling/"
LOGIN_TIMEOUT_MS = 300_000  # 5 minutes for manual MFA


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


def _try_feide_sso(page: Page) -> bool:
    """If on Feide org-selection page, click NTNU and wait for SSO redirect back to TP.

    This works silently as long as the stored Feide/NTNU/Microsoft session cookies
    are still valid (typically within hours of last MFA login).
    """
    if "auth.dataporten.no" not in page.url:
        return False

    try:
        # Set the hidden authselection field directly and submit - the <li> click
        # approach doesn't reliably enable the Continue button in headless-ish mode.
        page.evaluate("""() => {
            const inp = document.getElementById('org_selector');
            if (inp) inp.value = 'feide|realm|ntnu.no';
            const form = document.getElementById('discoform_feide');
            if (form) form.submit();
        }""")

        return _wait_for_tp_through_feide_chain(page)
    except Exception as e:
        logger.debug("Feide SSO failed: %s", e)
        return False


def _wait_for_tp_through_feide_chain(page: Page, timeout_ms: int = 90_000) -> bool:
    """Poll until TP is reached, auto-clicking through intermediate Feide/NTNU pages."""
    step_ms = 2_000
    elapsed = 0

    while elapsed < timeout_ms:
        page.wait_for_timeout(step_ms)
        elapsed += step_ms
        url = page.url

        if "tp.educloud.no" in url and not _is_login_page(page):
            page.wait_for_timeout(3000)
            return True

        # NTNU auth-method selection: choose Microsoft
        if "idp.feide.no" in url or "feide.no/simplesaml" in url:
            logger.debug("NTNU auth method page - clicking Microsoft")
            try:
                btn = page.get_by_text("Use work or school account", exact=False)
                if btn.count() > 0:
                    btn.first.click()
            except Exception:
                pass

    logger.debug("SSO chain timed out (URL: %s)", page.url)
    return False


def get_authenticated_context(p: Playwright) -> Tuple[Browser, BrowserContext, Page]:
    browser = p.chromium.launch(headless=False)

    if STATE_FILE.exists():
        ctx = browser.new_context(storage_state=str(STATE_FILE))
        page = ctx.new_page()
        page.goto(TARGET_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)  # let SPA fire initial XHRs and any auth redirects

        if _is_authenticated(page):
            print("Session restored from storage_state.json")
            return browser, ctx, page

        # SPA redirected to Feide org selection -- try silent SSO re-auth
        if "auth.dataporten.no" in page.url:
            logger.info("SPA redirected to Feide; trying silent SSO re-auth")
            if _try_feide_sso(page):
                print("Session restored via SSO re-auth")
                ctx.storage_state(path=str(STATE_FILE))
                return browser, ctx, page

        print("Session expired, starting fresh login...")
        page.close()
        ctx.close()

    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(TARGET_URL, wait_until="domcontentloaded")

    print("\nPlease complete Feide -> NTNU -> Microsoft Authenticator login in the browser window.")
    print("Waiting up to 5 minutes...\n")

    try:
        page.wait_for_url("**/tp.educloud.no/ntnu/app/rombestilling/**", timeout=LOGIN_TIMEOUT_MS)
    except Exception as e:
        raise RuntimeError(
            "Login timed out or failed. Make sure you approved the Authenticator push."
        ) from e

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
