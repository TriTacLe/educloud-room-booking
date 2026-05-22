from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Tuple

from playwright.sync_api import Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

STATE_FILE = Path("data/storage_state.json")
TARGET_URL = "https://tp.educloud.no/ntnu/app/rombestilling/"
LOGIN_TIMEOUT_MS = 300_000  # 5 minutes for MFA push


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
    """Fill username/email then password fields. Works on both Feide and Microsoft pages."""
    user = os.environ.get("FEIDE_USER", "")
    pwd = _get_feide_password(user)
    try:
        if user:
            # Feide uses input[name=username]; Microsoft uses input[type=email]
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


def _wait_for_tp_through_feide_chain(page: Page, timeout_ms: int = 90_000) -> bool:
    """Poll until TP is reached. Handles Feide org selector, NTNU method picker,
    and Microsoft email/password auto-fill along the way."""
    step_ms = 2_000
    elapsed = 0

    while elapsed < timeout_ms:
        page.wait_for_timeout(step_ms)
        elapsed += step_ms
        url = page.url

        if "tp.educloud.no" in url and not _is_login_page(page):
            page.wait_for_timeout(3000)
            return True

        # Feide org selector: pick NTNU
        if "auth.dataporten.no" in url:
            try:
                _submit_feide_org_selector(page)
            except Exception:
                pass

        # Feide/NTNU login: fill credentials OR click Microsoft method button
        if "idp.feide.no" in url or "feide.no/simplesaml" in url:
            _autofill_credentials(page)
            try:
                btn = page.get_by_text("Use work or school account", exact=False)
                if btn.count() > 0:
                    btn.first.click()
            except Exception:
                pass

        # Microsoft login: auto-fill email + password
        if "login.microsoftonline.com" in url:
            _autofill_credentials(page)

    logger.debug("SSO chain timed out (URL: %s)", page.url)
    return False


def _try_feide_sso(page: Page) -> bool:
    """If on Feide org-selection page, submit NTNU and wait for SSO redirect back to TP.

    Works silently when stored Microsoft/NTNU session cookies are still valid."""
    if "auth.dataporten.no" not in page.url:
        return False
    try:
        _submit_feide_org_selector(page)
        return _wait_for_tp_through_feide_chain(page)
    except Exception as e:
        logger.debug("Feide SSO failed: %s", e)
        return False


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

        print("Session expired, starting fresh login...")
        page.close()
        ctx.close()

    ctx = browser.new_context()
    page = ctx.new_page()
    page.goto(TARGET_URL, wait_until="domcontentloaded")

    print("\nApprove the Microsoft Authenticator push when prompted.\n")

    if not _wait_for_tp_through_feide_chain(page, timeout_ms=LOGIN_TIMEOUT_MS):
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
