from __future__ import annotations

from datetime import date as _date
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
    search_cutoff_start: str = "16:00"
    search_cutoff_end: str = "18:00"
    time_step_minutes: int = 15
    max_days_ahead: int = 14

    @field_validator("search_cutoff_start", "search_cutoff_end")
    @classmethod
    def check_cutoff_format(cls, v: str) -> str:
        parts = v.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise ValueError(f"time must be HH:MM, got '{v}'")
        return v

    @model_validator(mode="after")
    def check_dates(self) -> "BookingSpec":
        if not self.date and not self.dates and not self.schedule:
            raise ValueError("specify 'date', 'dates', or 'schedule'")
        return self

    def all_slots(self) -> list[tuple[_date, str, int]]:
        """Return list of (date, start_hhmm, count) tuples."""
        if self.schedule:
            return [(s.date, s.start, s.count) for s in self.schedule]
        dates = self.dates if self.dates else [self.date]  # type: ignore[list-item]
        return [(d, self.start, self.count) for d in dates]

    def all_dates(self) -> list[_date]:
        if self.dates:
            return self.dates
        if self.schedule:
            return [s.date for s in self.schedule]
        return [self.date]  # type: ignore[list-item]

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


class Config(BaseModel):
    account: Account = Account()
    booking: BookingSpec


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text())
    return Config.model_validate(raw)
