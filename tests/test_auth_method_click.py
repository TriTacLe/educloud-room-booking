"""Regression test for the Feide method-select page.

The Microsoft option on idp.feide.no is an <input type="submit"> whose label
lives in the value attribute, so text-based selectors never matched it and the
silent SSO chain stalled until timeout. The fixture is the real page captured
from a failed run (data/debug/20260713-055152-session-expired).
"""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from koker.auth import _click_account_tile, _click_microsoft_method

FIXTURES = Path(__file__).parent / "fixtures"


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
    page.set_content(
        (FIXTURES / "feide_method_select.html").read_text(), wait_until="domcontentloaded"
    )

    assert _click_microsoft_method(page) is True
    page.wait_for_timeout(500)
    assert submitted, "click did not submit the login-azuread form"


def test_clicks_remembered_account_on_chooser(page):
    """Dataporten account chooser: remembered login shown as a tile that must be
    clicked before OAuth continues. Fixture captured from a stalled run."""
    clicked = []
    page.route(
        "**/*",
        lambda route: (
            clicked.append(route.request.url)
            if "oauth/authorization" in route.request.url
            else None,
            route.abort(),
        ),
    )
    page.set_content(
        (FIXTURES / "dataporten_account_chooser.html").read_text(), wait_until="domcontentloaded"
    )

    assert _click_account_tile(page) is True
    page.wait_for_timeout(500)
    assert clicked, "tile click did not continue to oauth/authorization"
