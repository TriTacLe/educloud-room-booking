from datetime import date as real_date
from datetime import datetime as real_datetime
from datetime import timedelta

import pytest

import koker.rules as rules
from koker.rules import (
    RuleViolation,
    parse_hhmm,
    validate_duration,
    validate_lookahead,
    validate_time_window,
)


def test_parse_hhmm():
    assert parse_hhmm("09:30") == timedelta(hours=9, minutes=30)


def test_parse_hhmm_rejects_garbage():
    with pytest.raises(RuleViolation):
        parse_hhmm("not-a-time")


def test_validate_duration_ok():
    assert validate_duration("04:00") == timedelta(hours=4)


def test_validate_duration_zero():
    with pytest.raises(RuleViolation):
        validate_duration("00:00")


def test_validate_duration_over_limit():
    with pytest.raises(RuleViolation):
        validate_duration("04:01")


def test_validate_time_window_ok():
    validate_time_window("09:00", timedelta(hours=4))


def test_validate_time_window_before_opening():
    with pytest.raises(RuleViolation):
        validate_time_window("07:59", timedelta(hours=1))


def test_validate_time_window_past_closing():
    with pytest.raises(RuleViolation):
        validate_time_window("21:00", timedelta(hours=3))


def test_validate_lookahead_rejects_past_date():
    with pytest.raises(RuleViolation):
        validate_lookahead(real_date(2020, 1, 1))


def test_validate_lookahead_warns_beyond_quota(capsys):
    far = real_date.today() + timedelta(days=20)
    validate_lookahead(far)
    assert "future_days=14" in capsys.readouterr().err


def test_validate_lookahead_rejects_passed_start_today(monkeypatch):
    fixed_today = real_date(2026, 6, 15)

    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return fixed_today

    class FrozenDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 6, 15, 10, 0)

    monkeypatch.setattr(rules, "date", FrozenDate)
    monkeypatch.setattr(rules, "datetime", FrozenDatetime)

    with pytest.raises(RuleViolation):
        validate_lookahead(fixed_today, "09:00")

    validate_lookahead(fixed_today, "11:00")
