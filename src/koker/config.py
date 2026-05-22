from __future__ import annotations

import os
from datetime import date as _date, timedelta
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, field_validator, model_validator

from koker.rules import RuleViolation, validate_duration, validate_time_window


class Equipment(BaseModel):
    whiteboard: bool = False
    projector: bool = False


class SlotSpec(BaseModel):
    date: _date
    start: str
    count: int = 1

    @field_validator("start")
    @classmethod
    def check_start(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"start must be HH:MM, got '{v}'")
        return v


class BookingSpec(BaseModel):
    date: Optional[_date] = None
    dates: list[_date] = []
    days_ahead: Optional[int] = None  # book today + next N-1 days (dynamic, good for timers)
    schedule: list[SlotSpec] = []
    start: str = "09:00"
    duration: str  # HH:MM, max 04:00
    description: str = "Group work"
    notes: str = ""
    campus: str
    building: Optional[str] = None
    room_type: Optional[str] = None
    min_capacity: int = 1
    count: int = 1
    equipment: Equipment = Equipment()
    preferred_rooms: list[str] = []
    preferred_room_types: list[str] = []
    preferred_buildings: list[str] = []
    excluded_rooms: list[str] = []
    excluded_buildings: list[str] = []
    shuffle_rooms: bool = False
    end: str = "18:00"  # latest time a booking can end; search stops at end - duration
    time_step_minutes: int = 15
    max_days_ahead: int = 14
    no_overlap: bool = False

    @field_validator("end")
    @classmethod
    def check_end_format(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"time must be HH:MM, got '{v}'")
        return v

    @model_validator(mode="after")
    def check_dates(self) -> "BookingSpec":
        if not self.date and not self.dates and not self.schedule and self.days_ahead is None:
            raise ValueError("specify 'date', 'dates', 'days_ahead', or 'schedule'")
        return self

    def _resolved_dates(self) -> list[_date]:
        if self.days_ahead is not None:
            today = _date.today()
            return [today + timedelta(days=i) for i in range(self.days_ahead)]
        if self.dates:
            return self.dates
        return [self.date]  # type: ignore[list-item]

    def all_slots(self) -> list[tuple[_date, str, int]]:
        """Return list of (date, start_hhmm, count) tuples."""
        if self.schedule:
            return [(s.date, s.start, s.count) for s in self.schedule]
        return [(d, self.start, self.count) for d in self._resolved_dates()]

    def all_dates(self) -> list[_date]:
        if self.schedule:
            return [s.date for s in self.schedule]
        return self._resolved_dates()

    @field_validator("start")
    @classmethod
    def check_start_format(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"start must be HH:MM, got '{v}'")
        return v

    @field_validator("duration")
    @classmethod
    def check_duration_format(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"duration must be HH:MM, got '{v}'")
        return v

    @model_validator(mode="after")
    def check_rules(self) -> "BookingSpec":
        try:
            dur = validate_duration(self.duration)
            validate_time_window(self.start, dur)
        except RuleViolation as e:
            raise ValueError(str(e)) from e
        return self


class Account(BaseModel):
    feide_user: str = ""


class GmailConfig(BaseModel):
    to: str = ""
    from_addr: str = ""
    app_password: str = ""
    extra_to: list[str] = []

    def resolve(self) -> tuple[str, str, str]:
        to = self.to or os.getenv("GMAIL_TO", "")
        from_addr = self.from_addr or os.getenv("GMAIL_FROM", to)
        app_password = self.app_password or os.getenv("GMAIL_APP_PASSWORD", "")
        return to, from_addr, app_password

    def resolve_all_recipients(self) -> list[str]:
        primary = self.to or os.getenv("GMAIL_TO", "")
        recipients = [primary] if primary else []
        for addr in self.extra_to:
            if addr not in recipients:
                recipients.append(addr)
        return recipients


class GoogleCalendarConfig(BaseModel):
    calendar_id: str = "primary"
    credentials_file: str = "data/gcal_credentials.json"
    token_file: str = "data/gcal_token.json"
    attendees: list[str] = []  # extra invite addresses; GMAIL_TO is always included

    def resolve_attendees(self) -> list[str]:
        emails = list(self.attendees)
        gmail_to = os.getenv("GMAIL_TO", "")
        if gmail_to and gmail_to not in emails:
            emails.insert(0, gmail_to)
        return emails


class NotificationsConfig(BaseModel):
    gmail: Optional[GmailConfig] = None
    google_calendar: Optional[GoogleCalendarConfig] = None


class Config(BaseModel):
    account: Account = Account()
    booking: BookingSpec
    notifications: NotificationsConfig = NotificationsConfig()


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text())
    return Config.model_validate(raw)
