import sys
from datetime import date, datetime, timedelta


class RuleViolation(Exception):
    pass


def parse_hhmm(hhmm: str) -> timedelta:
    try:
        h, m = hhmm.split(":")
        return timedelta(hours=int(h), minutes=int(m))
    except (ValueError, AttributeError) as e:
        raise RuleViolation(f"Invalid HH:MM format '{hhmm}'") from e


def validate_duration(hhmm: str) -> timedelta:
    td = parse_hhmm(hhmm)
    if td <= timedelta(0):
        raise RuleViolation("Duration must be positive")
    if td > timedelta(hours=4):
        raise RuleViolation(f"Duration {hhmm} exceeds NTNU 4-hour limit")
    return td


def validate_time_window(start_hhmm: str, duration: timedelta) -> None:
    h, m = start_hhmm.split(":")
    start = timedelta(hours=int(h), minutes=int(m))
    earliest = timedelta(hours=8)
    latest = timedelta(hours=23)
    if start < earliest:
        raise RuleViolation(f"Start time {start_hhmm} is before 08:00")
    if start + duration > latest:
        end_h = int((start + duration).total_seconds() // 3600)
        end_m = int((start + duration).total_seconds() % 3600 // 60)
        raise RuleViolation(
            f"Booking ends at {end_h:02d}:{end_m:02d}, past NTNU 23:00 limit"
        )


def validate_lookahead(booking_date: date, start_hhmm: str | None = None) -> None:
    today = date.today()
    delta = (booking_date - today).days
    if delta < 0:
        raise RuleViolation(f"Date {booking_date} is in the past")
    if delta == 0 and start_hhmm is not None:
        h, m = start_hhmm.split(":")
        booking_dt = datetime.combine(today, datetime.min.time().replace(hour=int(h), minute=int(m)))
        if booking_dt <= datetime.now():
            raise RuleViolation(f"Start time {start_hhmm} today has already passed")
    if delta > 14:
        print(
            f"Warning: {booking_date} is {delta} days ahead (NTNU quota says future_days=14). "
            "Server may reject.",
            file=sys.stderr,
        )
