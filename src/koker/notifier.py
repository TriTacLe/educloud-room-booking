from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText

from koker.models import BookingResult

logger = logging.getLogger(__name__)


def send_alert(subject: str, body: str, to: str, app_password: str, from_addr: str = "") -> None:
    sender = from_addr or to
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(msg)

    logger.info("Alert sent to %s: %s", to, subject)


def send_gmail(result: BookingResult, to: str, app_password: str, from_addr: str = "") -> None:
    sender = from_addr or to
    end_minutes = result.start.hour * 60 + result.start.minute + result.duration_minutes
    end_h, end_m = divmod(end_minutes, 60)
    time_range = f"{result.start.strftime('%H:%M')}-{end_h:02d}:{end_m:02d}"

    lines = [
        f"Room:      {result.room.name}",
        f"Building:  {result.room.building}" if result.room.building else None,
        f"Type:      {result.room.room_type}" if result.room.room_type else None,
        f"Capacity:  {result.room.capacity}",
        f"Date:      {result.date.strftime('%d.%m.%Y')}",
        f"Time:      {time_range}",
        f"Booking ID: {result.booking_id}" if result.booking_id else None,
    ]
    body = "\n".join(line for line in lines if line is not None)

    msg = MIMEText(body)
    msg["Subject"] = f"Booked: {result.room.name} - {result.date.strftime('%d.%m.%Y')} {result.start.strftime('%H:%M')}"
    msg["From"] = sender
    msg["To"] = to

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, app_password)
        smtp.send_message(msg)

    logger.info("Gmail notification sent to %s", to)
