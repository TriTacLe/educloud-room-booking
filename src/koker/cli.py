from __future__ import annotations

import logging
import random
import sys
import time
from collections import defaultdict
from datetime import date as _date
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env", override=False)
from playwright.sync_api import sync_playwright

from koker.auth import get_authenticated_context, refresh_session
from koker.config import load_config
from koker.models import SearchCriteria
from koker.rules import (
    RuleViolation,
    validate_duration,
    validate_lookahead,
    validate_time_window,
)
from koker.tp_client import (
    BookingError,
    NoRoomsAvailableError,
    QuotaExceededError,
    SessionExpiredError,
    TPClient,
)

app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
DEBUG_DIR = Path("data/debug")


def _advance_time(t: dtime, step_minutes: int) -> dtime:
    dt = datetime.combine(_date.today(), t) + timedelta(minutes=step_minutes)
    return dt.time()


def _time_overlaps(start: dtime, duration_minutes: int, booked: list[tuple[dtime, dtime]]) -> bool:
    end = (datetime.combine(_date.today(), start) + timedelta(minutes=duration_minutes)).time()
    return any(start < b_end and b_start < end for b_start, b_end in booked)


def _latest_valid_start(duration_minutes: int, end: dtime) -> dtime:
    """Return the latest start time such that booking ends by `end`."""
    dur_td = timedelta(minutes=duration_minutes)
    end_dt = datetime.combine(_date.today(), end)
    return (end_dt - dur_td).time()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
        level=level,
    )


def _save_debug(page: object, label: str) -> None:
    from playwright.sync_api import Page as _Page

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = DEBUG_DIR / f"{ts}-{label}"
    out.mkdir(parents=True, exist_ok=True)
    try:
        p: _Page = page  # type: ignore[assignment]
        p.screenshot(path=str(out / "page.png"), full_page=True)
        (out / "page.html").write_text(p.content(), encoding="utf-8")
        print(f"Debug snapshot saved to {out}/")
    except Exception:
        pass


def _open_browser(p: object, headless: bool = True) -> tuple:
    browser, ctx, page = get_authenticated_context(p, headless=headless)  # type: ignore[arg-type]
    return browser, ctx, page


def _close_browser(browser: object, ctx: object, page: object) -> None:
    try:
        page.close()  # type: ignore[union-attr]
        ctx.close()  # type: ignore[union-attr]
    except Exception:
        pass
    try:
        browser.close()  # type: ignore[union-attr]
    except Exception:
        pass


@app.command()
def gcal_auth(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="Path to config.yaml"),
) -> None:
    """Authenticate with Google Calendar (one-time setup). Opens browser for OAuth."""
    _setup_logging(False)
    try:
        cfg = load_config(config)
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        raise typer.Exit(1)

    if not cfg.notifications.google_calendar:
        print("Add 'google_calendar: {}' under 'notifications:' in your config.", file=sys.stderr)
        raise typer.Exit(1)

    gcal_cfg = cfg.notifications.google_calendar
    try:
        from koker.gcal import auth
        auth(Path(gcal_cfg.credentials_file), Path(gcal_cfg.token_file))
        print(f"Done. Token saved to {gcal_cfg.token_file}")
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        raise typer.Exit(1)
    except Exception as e:
        print(f"Auth failed: {e}", file=sys.stderr)
        raise typer.Exit(1)


@app.command()
def login() -> None:
    """Open browser and complete Feide/NTNU login. Saves the session."""
    _setup_logging(False)
    with sync_playwright() as p:
        try:
            browser, ctx, page = _open_browser(p, headless=False)
            print("Login successful. Session stored in data/storage_state.json.")
            _close_browser(browser, ctx, page)
        except RuntimeError as e:
            print(f"Login failed: {e}", file=sys.stderr)
            raise typer.Exit(1)


@app.command()
def warm(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c"),
) -> None:
    """Refresh stored session without booking. Re-authenticates headlessly if expired."""
    _setup_logging(False)
    with sync_playwright() as p:
        browser, ctx, page = None, None, None
        try:
            # get_authenticated_context handles full re-auth (including MFA email) if expired
            browser, ctx, page = _open_browser(p)
            client = TPClient(page)
            client.get_my_bookings()
            refresh_session(ctx)
            print("Session is live, cookies refreshed.")
        except SessionExpiredError:
            print("Session still invalid after re-auth attempt.", file=sys.stderr)
            raise typer.Exit(1)
        except RuntimeError as e:
            print(f"Re-auth failed: {e}", file=sys.stderr)
            raise typer.Exit(1)
        finally:
            if browser:
                _close_browser(browser, ctx, page)


@app.command()
def status(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show current bookings and quota usage."""
    _setup_logging(verbose)

    with sync_playwright() as p:
        browser, ctx, page = None, None, None
        try:
            browser, ctx, page = _open_browser(p)
            client = TPClient(page)
            bookings = client.get_my_bookings()

            if not bookings:
                print("No active bookings found.")
            else:
                print(f"\nActive bookings ({len(bookings)} total):")
                print(f"{'#':<4} {'Room':<30} {'Date':<14} {'Time':<10} {'ID'}")
                print("-" * 70)
                for i, b in enumerate(bookings, 1):
                    print(f"{i:<4} {b.room_name:<30} {b.date_str:<14} {b.time_str:<10} {b.booking_id}")

        except SessionExpiredError:
            if page:
                _save_debug(page, "session-expired")
            print("Session expired. Run `koker login` again.", file=sys.stderr)
            raise typer.Exit(1)
        except Exception as e:
            if page:
                _save_debug(page, "status-error")
            logging.exception("Unexpected error")
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(1)
        finally:
            if browser:
                _close_browser(browser, ctx, page)


@app.command()
def cancel(
    all: bool = typer.Option(False, "--all", help="Cancel all active bookings"),
    count: Optional[int] = typer.Option(None, "--count", "-n", help="Cancel N bookings"),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="Path to config.yaml (for notifications)"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show active bookings. Use --all or --count N to cancel."""
    _setup_logging(verbose)

    cfg = None
    if config.exists():
        try:
            cfg = load_config(config)
        except Exception:
            pass

    with sync_playwright() as p:
        browser, ctx, page = None, None, None
        try:
            browser, ctx, page = _open_browser(p)
            client = TPClient(page)

            print("Fetching current bookings...")
            bookings = client.get_my_bookings()

            if not bookings:
                print("No active bookings found.")
                return

            print(f"\nActive bookings ({len(bookings)} total):")
            print(f"{'#':<4} {'Room':<30} {'Date':<14} {'Time':<10} {'ID'}")
            print("-" * 70)
            for i, b in enumerate(bookings, 1):
                print(f"{i:<4} {b.room_name:<30} {b.date_str:<14} {b.time_str:<10} {b.booking_id}")

            if not all and count is None:
                print("\nNo action taken. Use --all or --count N to cancel.")
                return

            to_cancel = bookings if all else bookings[:count]
            print(f"\nCancelling {len(to_cancel)} of {len(bookings)} booking(s)...")

            ids = [b.booking_id for b in to_cancel if b.booking_id]
            if ids:
                n = client.cancel_n_bookings(ids)
            else:
                n = client.cancel_all_bookings(limit=None if all else count)

            print(f"Cancelled {n} booking(s).")
            refresh_session(ctx)

            if cfg and cfg.notifications.gmail and n > 0:
                try:
                    from koker.notifier import send_alert
                    to_addr, from_addr, app_password = cfg.notifications.gmail.resolve()
                    if to_addr and app_password:
                        lines = [f"Cancelled {n} booking(s):", ""]
                        for b in to_cancel:
                            lines.append(f"  {b.room_name}  {b.date_str}  {b.time_str}  ({b.booking_id})")
                        send_alert(
                            subject=f"koker: cancelled {n} booking(s)",
                            body="\n".join(lines),
                            to=to_addr,
                            app_password=app_password,
                            from_addr=from_addr,
                        )
                except Exception as notify_err:
                    print(f"Warning: Gmail notification failed: {notify_err}", file=sys.stderr)

        except SessionExpiredError:
            if page:
                _save_debug(page, "session-expired")
            print("Session expired. Run `koker login` again.", file=sys.stderr)
            raise typer.Exit(1)
        except Exception as e:
            if page:
                _save_debug(page, "cancel-error")
            logging.exception("Unexpected error")
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(1)
        finally:
            if browser:
                _close_browser(browser, ctx, page)


@app.command()
def book(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="Path to config.yaml"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    rollback: bool = typer.Option(False, "--rollback", help="Cancel all bookings made in this run on failure"),
    timeout: Optional[int] = typer.Option(None, "--timeout", "-t", help="Max run time in minutes. Exits cleanly when reached."),
    stall_timeout: int = typer.Option(20, "--stall-timeout", "-s", help="Exit if no booking succeeds for this many minutes (0 = disabled)."),
) -> None:
    """Book rooms from config.yaml. Supports schedule, dates list, or single date."""
    _setup_logging(verbose)

    if not config.exists():
        print(f"Config not found: {config}", file=sys.stderr)
        print("Copy config.example.yaml to config.yaml and fill in your booking details.")
        raise typer.Exit(1)

    try:
        cfg = load_config(config)
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        raise typer.Exit(1)

    spec = cfg.booking
    try:
        duration_td = validate_duration(spec.duration)
        validate_time_window(spec.start, duration_td)
    except RuleViolation as e:
        print(f"Rule violation: {e}", file=sys.stderr)
        raise typer.Exit(1)

    duration_minutes = int(duration_td.total_seconds() // 60)

    skipped: set[tuple] = set()
    for slot_date, slot_start, _ in spec.all_slots():
        try:
            validate_lookahead(slot_date, slot_start)
        except RuleViolation as e:
            print(f"Skipping slot {slot_date} {slot_start}: {e}", file=sys.stderr)
            skipped.add((slot_date, slot_start))

    slots = [(d, s, c) for d, s, c in spec.all_slots() if (d, s) not in skipped]
    total_bookings = sum(count for _, _, count in slots)

    end_h, end_m = (int(x) for x in spec.end.split(":"))
    end_time = dtime(end_h, end_m)
    latest_start = _latest_valid_start(duration_minutes, end_time)

    with sync_playwright() as p:
        browser, ctx, page = None, None, None
        booked_ids: list[str] = []

        try:
            browser, ctx, page = _open_browser(p)
            client = TPClient(page)

            deadline = time.monotonic() + timeout * 60 if timeout else None
            stall_deadline_s = stall_timeout * 60 if stall_timeout else None
            last_progress = time.monotonic()

            print(f"\nBooking up to {total_bookings} room(s) across {len(slots)} slot(s) at {spec.campus} (round-robin)")

            any_success = False
            booking_num = 0
            failed = False
            quota_hit = False
            timed_out = False
            booked_per_slot: dict[tuple[_date, str], set[str]] = defaultdict(set)
            booked_times: dict[_date, list[tuple[dtime, dtime]]] = defaultdict(list)
            all_results: list = []

            if spec.no_overlap:
                try:
                    existing = client.get_my_bookings()
                    for b in existing:
                        if not b.date_str or not b.time_str:
                            continue
                        try:
                            bdate = datetime.strptime(b.date_str, "%d.%m.%Y").date()
                            bstart = dtime(*map(int, b.time_str.split(":")))
                            if b.end_str:
                                bend = dtime(*map(int, b.end_str.split(":")))
                            else:
                                bend = (datetime.combine(_date.today(), bstart) + timedelta(minutes=duration_minutes)).time()
                            booked_times[bdate].append((bstart, bend))
                        except Exception:
                            pass
                    if existing:
                        logging.info("Loaded %d existing booking(s) for overlap check", len(existing))
                except Exception:
                    pass

            # Pre-compute slot metadata; round-robin distributes 1 booking per slot per round
            # so count:100 fills quota evenly across all days instead of front-loading day 1.
            # days_ahead mode: no date overflow per slot - all days already in queue
            overflow_days = 0 if spec.days_ahead is not None else spec.max_days_ahead
            slot_meta: list[tuple[_date, dtime, int, _date]] = []
            for slot_date, slot_start, slot_count in slots:
                start_h, start_m = (int(x) for x in slot_start.split(":"))
                slot_start_time = dtime(start_h, start_m)
                max_search_date = slot_date + timedelta(days=overflow_days)
                slot_meta.append((slot_date, slot_start_time, slot_count, max_search_date))

            max_count = max((s[2] for s in slot_meta), default=0)

            for round_num in range(max_count):
                if failed or timed_out:
                    break
                for slot_date, slot_start_time, slot_count, max_search_date in slot_meta:
                    if failed or timed_out:
                        break
                    if round_num >= slot_count:
                        continue

                    booking_num += 1
                    booking_found = False
                    search_date = slot_date

                    while search_date <= max_search_date and not booking_found:
                        if spec.shuffle_rooms:
                            # Vary start time per date so different hours get sampled,
                            # exposing rooms that are only free in the afternoon.
                            start_dt = datetime.combine(_date.today(), slot_start_time)
                            end_dt = datetime.combine(_date.today(), latest_start)
                            total_steps = max(0, int((end_dt - start_dt).total_seconds() / 60) // spec.time_step_minutes)
                            offset = random.randint(0, total_steps) * spec.time_step_minutes
                            search_start = _advance_time(slot_start_time, offset)
                        else:
                            search_start = slot_start_time

                        while search_start <= latest_start and not booking_found:
                            if deadline is not None and time.monotonic() > deadline:
                                print(f"Timeout ({timeout}m) reached, stopping.", file=sys.stderr)
                                timed_out = True
                                break
                            if stall_deadline_s is not None and time.monotonic() - last_progress > stall_deadline_s:
                                print(f"No booking found in {stall_timeout}m, stopping.", file=sys.stderr)
                                timed_out = True
                                break
                            if spec.no_overlap and _time_overlaps(search_start, duration_minutes, booked_times[search_date]):
                                search_start = _advance_time(search_start, spec.time_step_minutes)
                                continue

                            slot_key = (search_date, search_start.strftime("%H:%M"))
                            already_booked = booked_per_slot[slot_key]

                            criteria = SearchCriteria(
                                date=search_date,
                                start=search_start,
                                duration_minutes=duration_minutes,
                                area_label=spec.campus,
                                building_label=spec.building or "",
                                room_type_label=spec.room_type or "",
                                min_capacity=spec.min_capacity,
                                preferred_rooms=spec.preferred_rooms,
                                preferred_room_types=spec.preferred_room_types,
                                preferred_buildings=spec.preferred_buildings,
                                excluded_rooms=spec.excluded_rooms,
                                excluded_buildings=spec.excluded_buildings,
                                shuffle_rooms=spec.shuffle_rooms,
                                description=spec.description,
                                notes=spec.notes,
                            )

                            try:
                                all_rooms = client.search(criteria)
                            except NoRoomsAvailableError:
                                search_start = _advance_time(search_start, spec.time_step_minutes)
                                continue

                            available = [r for r in all_rooms if r.name not in already_booked]
                            if not available:
                                search_start = _advance_time(search_start, spec.time_step_minutes)
                                continue

                            room = client.pick_room(available, criteria)
                            if room is None:
                                search_start = _advance_time(search_start, spec.time_step_minutes)
                                continue

                            type_tag = f" [{room.room_type}]" if room.room_type else ""
                            print(f"[{booking_num}] Selected: {room.name}{type_tag} (cap {room.capacity}) on {search_date} at {search_start.strftime('%H:%M')}")

                            try:
                                result = client.confirm(room, criteria)
                            except QuotaExceededError:
                                print(f"[{booking_num}] Quota exceeded.", file=sys.stderr)
                                failed = True
                                quota_hit = True
                                break

                            if result.success:
                                end_mins = result.start.hour * 60 + result.start.minute + result.duration_minutes
                                end_h2, end_m2 = divmod(end_mins, 60)
                                time_range = f"{result.start.strftime('%H:%M')}-{end_h2:02d}:{end_m2:02d}"
                                print(f"[{booking_num}] Booked: {result.room.name} on {result.date} {time_range}")
                                booked_per_slot[slot_key].add(room.name)
                                booking_end = (datetime.combine(_date.today(), search_start) + timedelta(minutes=duration_minutes)).time()
                                booked_times[search_date].append((search_start, booking_end))
                                if result.booking_id:
                                    booked_ids.append(result.booking_id)
                                all_results.append(result)
                                any_success = True
                                booking_found = True
                                last_progress = time.monotonic()
                                if cfg.notifications.google_calendar:
                                    try:
                                        from koker.gcal import create_event
                                        gcal_cfg = cfg.notifications.google_calendar
                                        create_event(result, gcal_cfg.calendar_id, Path(gcal_cfg.credentials_file), Path(gcal_cfg.token_file), gcal_cfg.resolve_attendees())
                                    except Exception as gcal_err:
                                        print(f"[{booking_num}] Warning: Google Calendar event failed: {gcal_err}", file=sys.stderr)
                            else:
                                print(f"[{booking_num}] Warning: {result.message}")
                                search_start = _advance_time(search_start, spec.time_step_minutes)

                        if not booking_found and not failed:
                            search_date += timedelta(days=1)
                            if search_date <= max_search_date:
                                print(f"[{booking_num}] No room on {search_date - timedelta(days=1)}, trying {search_date}...")

                    if not booking_found and not failed:
                        print(f"[{booking_num}] No room found within search window.", file=sys.stderr)

            client.flush_xhr_log(DEBUG_DIR)
            if all_results and cfg.notifications.gmail:
                try:
                    from koker.notifier import send_summary
                    _, from_addr, app_password = cfg.notifications.gmail.resolve()
                    all_recipients = cfg.notifications.gmail.resolve_all_recipients()
                    if all_recipients and app_password:
                        send_summary(all_results, all_recipients, app_password, from_addr)
                    else:
                        print("Warning: summary email skipped - GMAIL_TO or GMAIL_APP_PASSWORD not set", file=sys.stderr)
                except Exception as notify_err:
                    print(f"Warning: summary email failed: {notify_err}", file=sys.stderr)
            if any_success:
                refresh_session(ctx)

            if quota_hit and cfg.notifications.gmail:
                try:
                    from koker.notifier import send_alert
                    to, from_addr, app_password = cfg.notifications.gmail.resolve()
                    if to and app_password:
                        send_alert(
                            subject=f"koker: quota reached ({booking_num - 1} booked)",
                            body=f"NTNU booking quota exhausted after {booking_num - 1} booking(s). No further bookings will be made until the quota resets.",
                            to=to,
                            app_password=app_password,
                            from_addr=from_addr,
                        )
                except Exception:
                    pass

            if timed_out and not any_success and cfg.notifications.gmail:
                try:
                    from koker.notifier import send_alert
                    to, from_addr, app_password = cfg.notifications.gmail.resolve()
                    if to and app_password:
                        send_alert(
                            subject="koker: no rooms found - stall timeout",
                            body=f"No room could be booked after {stall_timeout}m of searching. All slots may be full, or the search window is too narrow.",
                            to=to,
                            app_password=app_password,
                            from_addr=from_addr,
                        )
                except Exception:
                    pass

        except SessionExpiredError:
            if page:
                _save_debug(page, "session-expired")
            msg = "Session expired. Delete data/storage_state.json and run `koker login` again."
            print(msg, file=sys.stderr)
            if cfg.notifications.gmail:
                try:
                    from koker.notifier import send_alert
                    to, from_addr, app_password = cfg.notifications.gmail.resolve()
                    if to and app_password:
                        send_alert(
                            subject="koker: session expired - action required",
                            body="NTNU room booking session expired.\n\nRun `koker login` to re-authenticate, then the timer will resume booking automatically.",
                            to=to,
                            app_password=app_password,
                            from_addr=from_addr,
                        )
                except Exception:
                    pass
            raise typer.Exit(1)

        except QuotaExceededError:
            if page:
                _save_debug(page, "quota-exceeded")
            print("NTNU quota reached.", file=sys.stderr)
            if cfg.notifications.gmail:
                try:
                    from koker.notifier import send_alert
                    to, from_addr, app_password = cfg.notifications.gmail.resolve()
                    if to and app_password:
                        send_alert(
                            subject="koker: quota reached - no more bookings",
                            body="NTNU booking quota is full. No further bookings will be made until the quota resets.",
                            to=to,
                            app_password=app_password,
                            from_addr=from_addr,
                        )
                except Exception:
                    pass
            raise typer.Exit(1)

        except NoRoomsAvailableError as e:
            if page:
                _save_debug(page, "no-rooms")
            print(f"No rooms available: {e}", file=sys.stderr)
            if cfg.notifications.gmail:
                try:
                    from koker.notifier import send_alert
                    to, from_addr, app_password = cfg.notifications.gmail.resolve()
                    if to and app_password:
                        send_alert(
                            subject="koker: no rooms available",
                            body=f"No rooms found for any slot: {e}",
                            to=to,
                            app_password=app_password,
                            from_addr=from_addr,
                        )
                except Exception:
                    pass
            raise typer.Exit(1)

        except BookingError as e:
            if page:
                _save_debug(page, "booking-error")
            print(f"Booking error: {e}", file=sys.stderr)
            if rollback and booked_ids and page:
                print("Rolling back bookings made in this run...")
                try:
                    client = TPClient(page)  # type: ignore[possibly-undefined]
                    cancelled = client.cancel_all_bookings()
                    print(f"Rolled back {cancelled} booking(s).")
                except Exception:
                    pass
            if cfg.notifications.gmail:
                try:
                    from koker.notifier import send_alert
                    to, from_addr, app_password = cfg.notifications.gmail.resolve()
                    if to and app_password:
                        send_alert(
                            subject="koker: booking error",
                            body=f"Booking failed with error:\n\n{e}",
                            to=to,
                            app_password=app_password,
                            from_addr=from_addr,
                        )
                except Exception:
                    pass
            raise typer.Exit(1)

        except Exception as e:
            if page:
                _save_debug(page, "unexpected-error")
            logging.exception("Unexpected error")
            print(f"Unexpected error: {e}", file=sys.stderr)
            if cfg.notifications.gmail:
                try:
                    from koker.notifier import send_alert
                    to, from_addr, app_password = cfg.notifications.gmail.resolve()
                    if to and app_password:
                        send_alert(
                            subject="koker: unexpected error",
                            body=f"koker crashed with an unexpected error:\n\n{type(e).__name__}: {e}\n\nCheck logs for full traceback.",
                            to=to,
                            app_password=app_password,
                            from_addr=from_addr,
                        )
                except Exception:
                    pass
            raise typer.Exit(1)

        finally:
            if ctx and page:
                try:
                    page.close()
                    ctx.close()
                except Exception:
                    pass
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass


@app.command()
def rooms(
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", help="Path to config.yaml"),
    date_str: Optional[str] = typer.Option(None, "--date", "-d", help="Date to search (YYYY-MM-DD). Defaults to tomorrow."),
    start: Optional[str] = typer.Option(None, "--start", "-s", help="Start time HH:MM. Defaults to config start."),
) -> None:
    """List available rooms matching the config criteria for a given date."""
    if not config.exists():
        print(f"Config not found: {config}", file=sys.stderr)
        raise typer.Exit(1)

    try:
        cfg = load_config(config)
    except Exception as e:
        print(f"Config error: {e}", file=sys.stderr)
        raise typer.Exit(1)

    spec = cfg.booking

    if date_str:
        search_date = _date.fromisoformat(date_str)
    else:
        search_date = _date.today() + timedelta(days=1)

    start_str = start or spec.start
    start_h, start_m = (int(x) for x in start_str.split(":"))
    search_start = dtime(start_h, start_m)

    duration_td = validate_duration(spec.duration)
    duration_minutes = int(duration_td.total_seconds() // 60)

    criteria = SearchCriteria(
        date=search_date,
        start=search_start,
        duration_minutes=duration_minutes,
        area_label=spec.campus,
        building_label=spec.building or "",
        room_type_label=spec.room_type or "",
        min_capacity=spec.min_capacity,
        preferred_rooms=spec.preferred_rooms,
        preferred_room_types=spec.preferred_room_types,
        preferred_buildings=spec.preferred_buildings,
        excluded_rooms=spec.excluded_rooms,
        excluded_buildings=spec.excluded_buildings,
        shuffle_rooms=False,
        description=spec.description,
        notes=spec.notes,
    )

    with sync_playwright() as p:
        browser, ctx, page = None, None, None
        try:
            browser, ctx, page = _open_browser(p)
            client = TPClient(page)
            available = client.search(criteria)
            full = client.search_full(criteria)

            header = f"Rooms on {search_date} at {search_start.strftime('%H:%M')} ({duration_minutes // 60}h) - {spec.campus}, cap {spec.min_capacity}+"
            col = f"{'#':<4} {'Room':<12} {'Type':<20} {'Cap':<6} {'Building'}"
            sep = "-" * 70

            print(f"\nAvailable ({len(available)}): {header}")
            print(col)
            print(sep)
            for i, r in enumerate(available, 1):
                print(f"{i:<4} {r.name:<12} {r.room_type:<20} {r.capacity:<6} {r.building}")
            if not available:
                print("  (none)")

            print(f"\nFull / unbookable ({len(full)}):")
            print(col)
            print(sep)
            for i, r in enumerate(full, 1):
                print(f"{i:<4} {r.name:<12} {r.room_type:<20} {r.capacity:<6} {r.building}")
            if not full:
                print("  (none)")

        except NoRoomsAvailableError:
            print(f"No rooms available on {search_date} at {search_start.strftime('%H:%M')}.")
        except SessionExpiredError:
            print("Session expired. Run `koker login` again.", file=sys.stderr)
            raise typer.Exit(1)
        except Exception as e:
            logging.exception("Unexpected error")
            print(f"Error: {e}", file=sys.stderr)
            raise typer.Exit(1)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
