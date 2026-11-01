from datetime import date, time

import koker.notifier as notifier
from koker.models import BookingResult, Room


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, *args, **kwargs):
        self.sent = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, *args, **kwargs):
        pass

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent = (from_addr, to_addrs, msg)


def make_result(
    day, hour, minute, name, capacity, duration_minutes=240, booking_id="", building=""
):
    return BookingResult(
        success=True,
        room=Room(id=name, name=name, capacity=capacity, building=building),
        date=day,
        start=time(hour, minute),
        duration_minutes=duration_minutes,
        booking_id=booking_id,
    )


def test_send_summary_composes_sorted_body(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)

    later = make_result(
        date(2026, 5, 12), 9, 0, "R8", 6, booking_id="abc", building="Realfagbygget"
    )
    earlier = make_result(date(2026, 5, 11), 13, 0, "G138", 10)

    notifier.send_summary([later, earlier], "to@example.com", "app-password", "from@example.com")

    assert len(FakeSMTP.instances) == 1
    from_addr, to_addrs, raw_msg = FakeSMTP.instances[0].sent
    assert from_addr == "from@example.com"
    assert to_addrs == ["to@example.com"]
    assert "koker: booked 2 room(s)" in raw_msg

    earlier_pos = raw_msg.index("11.05.2026")
    later_pos = raw_msg.index("12.05.2026")
    assert earlier_pos < later_pos
    assert "13:00-17:00" in raw_msg
    assert "G138 (cap 10)" in raw_msg
    assert "R8 (cap 6), Realfagbygget" in raw_msg
    assert "ID: abc" in raw_msg


def test_send_summary_skips_empty_results(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)

    notifier.send_summary([], "to@example.com", "app-password")

    assert FakeSMTP.instances == []


def test_send_summary_skips_without_recipients(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(notifier.smtplib, "SMTP_SSL", FakeSMTP)

    result = make_result(date(2026, 5, 11), 9, 0, "G138", 10)
    notifier.send_summary([result], [], "app-password")

    assert FakeSMTP.instances == []
