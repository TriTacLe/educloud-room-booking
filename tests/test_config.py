from datetime import date

import pytest
from pydantic import ValidationError

from koker.config import BookingSpec, GmailConfig, GoogleCalendarConfig, SlotSpec, load_config


def test_booking_spec_requires_a_date_source():
    with pytest.raises(ValidationError):
        BookingSpec(duration="04:00", campus="Gloshaugen")


def test_booking_spec_rejects_over_limit_duration():
    with pytest.raises(ValidationError):
        BookingSpec(duration="05:00", campus="Gloshaugen", days_ahead=1)


def test_booking_spec_rejects_bad_start_format():
    with pytest.raises(ValidationError):
        BookingSpec(duration="04:00", campus="Gloshaugen", days_ahead=1, start="0900")


def test_all_slots_from_days_ahead():
    spec = BookingSpec(duration="04:00", campus="Gloshaugen", days_ahead=3, start="09:00", count=2)
    slots = spec.all_slots()
    assert len(slots) == 3
    assert all(start == "09:00" and count == 2 for _date, start, count in slots)


def test_all_slots_from_schedule():
    spec = BookingSpec(
        duration="04:00",
        campus="Gloshaugen",
        schedule=[SlotSpec(date=date(2026, 5, 11), start="10:00", count=2)],
    )
    assert spec.all_slots() == [(date(2026, 5, 11), "10:00", 2)]
    assert spec.all_dates() == [date(2026, 5, 11)]


def test_gmail_config_resolve_all_recipients_dedupes(monkeypatch):
    monkeypatch.setenv("GMAIL_TO", "primary@example.com")
    cfg = GmailConfig(extra_to=["extra@example.com", "primary@example.com"])
    assert cfg.resolve_all_recipients() == ["primary@example.com", "extra@example.com"]


def test_gmail_config_resolve_all_recipients_no_primary(monkeypatch):
    monkeypatch.delenv("GMAIL_TO", raising=False)
    cfg = GmailConfig(extra_to=["extra@example.com"])
    assert cfg.resolve_all_recipients() == ["extra@example.com"]


def test_google_calendar_config_resolve_attendees_inserts_gmail_to_first(monkeypatch):
    monkeypatch.setenv("GMAIL_TO", "primary@example.com")
    cfg = GoogleCalendarConfig(attendees=["other@example.com"])
    assert cfg.resolve_attendees() == ["primary@example.com", "other@example.com"]


def test_google_calendar_config_resolve_attendees_no_duplicate(monkeypatch):
    monkeypatch.setenv("GMAIL_TO", "primary@example.com")
    cfg = GoogleCalendarConfig(attendees=["primary@example.com"])
    assert cfg.resolve_attendees() == ["primary@example.com"]


def test_load_config_from_yaml(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
booking:
  duration: "04:00"
  campus: Gloshaugen
  days_ahead: 2
"""
    )
    cfg = load_config(config_file)
    assert cfg.booking.campus == "Gloshaugen"
    assert cfg.booking.days_ahead == 2
