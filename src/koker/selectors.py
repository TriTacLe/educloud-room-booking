"""
Central Playwright locator registry for TP v127 (NTNU).
All selectors confirmed against the live English-language SPA.
When TP ships a UI update, fix here only.
"""

from playwright.sync_api import Locator, Page


def date_input(page: Page) -> Locator:
    return page.get_by_role("textbox", name="DD.MM.YYYY")


def start_time_input(page: Page) -> Locator:
    # Two hh:mm textboxes; first = start, second = end
    return page.get_by_role("textbox", name="hh:mm").first


def end_time_input(page: Page) -> Locator:
    return page.get_by_role("textbox", name="hh:mm").nth(1)


def area_combobox(page: Page) -> Locator:
    return page.get_by_role("combobox", name="Choose area")


def building_combobox(page: Page) -> Locator:
    return page.get_by_role("combobox", name="Choose building")


def room_type_combobox(page: Page) -> Locator:
    return page.get_by_role("combobox", name="Choose room type")


def capacity_input(page: Page) -> Locator:
    return page.get_by_role("spinbutton", name="Minimum amount of seats")


def description_field(page: Page) -> Locator:
    return page.get_by_role("textbox", name="Description")


def notes_field(page: Page) -> Locator:
    return page.get_by_role("textbox", name="Notes")


def confirm_button(page: Page) -> Locator:
    return page.get_by_role("button", name="Confirm booking")
