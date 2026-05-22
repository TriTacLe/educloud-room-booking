from __future__ import annotations

import logging
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from koker.models import BookingResult

logger = logging.getLogger(__name__)


def send_mfa_number(number: str, screenshot: bytes, to: str | list[str], app_password: str, from_addr: str = "") -> None:
    recipients = [to] if isinstance(to, str) else to
    sender = from_addr or recipients[0]

    msg = MIMEMultipart()
    msg["Subject"] = f"koker: approve MFA - tap {number}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    body = MIMEText(
        f"Open Microsoft Authenticator and tap the number: {number}\n\n"
        "Screenshot attached - booking runs automatically after approval."
    )
    msg.attach(body)

    if screenshot:
        img = MIMEImage(screenshot, name="mfa.png")
        msg.attach(img)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, app_password)
        smtp.sendmail(sender, recipients, msg.as_string())

    logger.info("MFA number email sent: %s", number)


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


def send_summary(results: list[BookingResult], to: str | list[str], app_password: str, from_addr: str = "") -> None:
    recipients = [to] if isinstance(to, str) else to
    if not recipients or not results:
        return
    sender = from_addr or recipients[0]

    lines = [f"Booked {len(results)} room(s):", ""]
    for r in sorted(results, key=lambda x: (x.date, x.start)):
        end_minutes = r.start.hour * 60 + r.start.minute + r.duration_minutes
        end_h, end_m = divmod(end_minutes, 60)
        time_range = f"{r.start.strftime('%H:%M')}-{end_h:02d}:{end_m:02d}"
        line = f"{r.date.strftime('%d.%m.%Y')}  {time_range}  {r.room.name} (cap {r.room.capacity})"
        if r.room.building:
            line += f", {r.room.building}"
        lines.append(line)
        if r.booking_id:
            lines.append(f"  ID: {r.booking_id}")

    body = "\n".join(lines)
    msg = MIMEText(body)
    msg["Subject"] = f"koker: booked {len(results)} room(s)"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, app_password)
        smtp.sendmail(sender, recipients, msg.as_string())

    logger.info("Summary email sent to %s: %d booking(s)", ", ".join(recipients), len(results))
