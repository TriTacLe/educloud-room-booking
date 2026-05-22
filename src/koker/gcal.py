from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

from koker.models import BookingResult

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _copy_to_clipboard(text: str) -> bool:
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
        try:
            subprocess.run(cmd, input=text.encode(), check=True, capture_output=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return False


def _get_creds(credentials_path: Path, token_path: Path, interactive: bool = False):  # type: ignore[return]
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                raise FileNotFoundError(
                    f"Google credentials not found: {credentials_path}\n"
                    "Download from Google Cloud Console > APIs & Services > Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            # Disable PKCE - conflicts with access_type=offline on Google's OAuth endpoint
            flow.oauth2session.create_pkce_tokens = lambda: None  # type: ignore[method-assign]

            if interactive:
                # Intercept the auth URL so we can print + copy it before the browser opens
                _captured: list[str] = []
                _orig_auth_url = flow.authorization_url

                def _capturing(**kwargs: object) -> tuple[str, str]:
                    url, state = _orig_auth_url(**kwargs)
                    _captured.append(url)
                    return url, state

                flow.authorization_url = _capturing  # type: ignore[method-assign]

                def _announce() -> None:
                    for _ in range(100):
                        if _captured:
                            url = _captured[0]
                            copied = _copy_to_clipboard(url)
                            print(f"\nAuth URL{' (copied to clipboard)' if copied else ''}:\n{url}\n")
                            return
                        threading.Event().wait(0.05)

                t = threading.Thread(target=_announce, daemon=True)
                t.start()

            creds = flow.run_local_server(
                port=0,
                prompt="consent",
                authorization_prompt_message="Opening browser for Google auth...\n",
            )
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
    return creds


def auth(credentials_path: Path, token_path: Path) -> None:
    """Run OAuth2 browser flow and save token. Call once."""
    _get_creds(credentials_path, token_path, interactive=True)
    logger.info("Google Calendar auth complete. Token saved to %s", token_path)


def create_event(
    result: BookingResult,
    calendar_id: str,
    credentials_path: Path,
    token_path: Path,
    attendees: list[str] | None = None,
) -> str:
    """Create a Google Calendar event for a booking. Returns event ID."""
    from googleapiclient.discovery import build

    creds = _get_creds(credentials_path, token_path)
    service = build("calendar", "v3", credentials=creds)

    end_minutes = result.start.hour * 60 + result.start.minute + result.duration_minutes
    end_h, end_m = divmod(end_minutes, 60)

    date_str = result.date.isoformat()
    start_dt = f"{date_str}T{result.start.strftime('%H:%M')}:00"
    end_dt = f"{date_str}T{end_h:02d}:{end_m:02d}:00"

    location_parts = [result.room.name]
    if result.room.building:
        location_parts.append(result.room.building)
    location_parts.append("NTNU Gloshaug, Trondheim")

    desc_lines = [
        f"Room: {result.room.name}",
        f"Building: {result.room.building}" if result.room.building else None,
        f"Type: {result.room.room_type}" if result.room.room_type else None,
        f"Capacity: {result.room.capacity}",
        f"Booking ID: {result.booking_id}" if result.booking_id else None,
    ]
    description = "\n".join(l for l in desc_lines if l)

    event: dict = {
        "summary": f"{result.room.name} - NTNU",
        "location": ", ".join(location_parts),
        "description": description,
        "start": {"dateTime": start_dt, "timeZone": "Europe/Oslo"},
        "end": {"dateTime": end_dt, "timeZone": "Europe/Oslo"},
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": 30}],
        },
    }

    if attendees:
        event["attendees"] = [{"email": a} for a in attendees]

    send_updates = "all" if attendees else "none"
    created = service.events().insert(calendarId=calendar_id, body=event, sendUpdates=send_updates).execute()
    event_id = created.get("id", "")
    logger.info("Calendar event created: %s", created.get("htmlLink", event_id))
    return event_id
