from dataclasses import dataclass, field
from datetime import date, time


@dataclass
class Room:
    id: str
    name: str
    capacity: int
    building: str = ""
    room_type: str = ""


@dataclass
class BookingResult:
    success: bool
    room: Room
    date: date
    start: time
    duration_minutes: int
    booking_id: str = ""
    message: str = ""


@dataclass
class MyBooking:
    booking_id: str
    room_name: str
    date_str: str
    time_str: str
    raw: str = ""


@dataclass
class SearchCriteria:
    date: date
    start: time
    duration_minutes: int
    area_label: str
    building_label: str = ""
    room_type_label: str = ""
    min_capacity: int = 1
    preferred_rooms: list[str] = field(default_factory=list)
    preferred_room_types: list[str] = field(default_factory=list)
    description: str = "Group work"
    notes: str = ""
