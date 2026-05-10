import os
from datetime import date, time

from koker.models import BookingResult, Room
from koker.notifier import send_gmail

result = BookingResult(
    success=True,
    room=Room(id="test", name="R8 Realfagbygget", capacity=6, building="Realfagbygget", room_type="Group room"),
    date=date(2026, 5, 21),
    start=time(9, 0),
    duration_minutes=240,
    booking_id="test-123",
)

send_gmail(result, os.environ["GMAIL_TO"], os.environ["GMAIL_APP_PASSWORD"])
print("Sent.")
