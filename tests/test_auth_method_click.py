"""Regression test for the Feide method-select page.

The Microsoft option on idp.feide.no is an <input type="submit"> whose label
lives in the value attribute, so text-based selectors never matched it and the
silent SSO chain stalled until timeout. The fixture is the real page captured
from a failed run (data/debug/20260713-055152-session-expired).
"""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from koker.auth import _click_microsoft_method

FIXTURE = Path(__file__).parent / "fixtures" / "feide_method_select.html"


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


def test_clicks_microsoft_submit_on_feide_page(page):
    submitted = []
    page.route(
        "**/*",
        lambda route: (
            submitted.append(route.request.url)
            if "login-azuread" in route.request.url
            else None,
            route.abort(),
        ),
    )
    page.set_content(FIXTURE.read_text(), wait_until="domcontentloaded")

    assert _click_microsoft_method(page) is True
    page.wait_for_timeout(500)
    assert submitted, "click did not submit the login-azuread form"
