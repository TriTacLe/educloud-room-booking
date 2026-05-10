from __future__ import annotations

import logging
import sys
from collections import defaultdict
from datetime import date as _date
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Optional

import typer
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


def _latest_valid_start(duration_minutes: int, cutoff_start: dtime, cutoff_end: dtime) -> dtime:
    """Return the latest start time such that booking ends by cutoff_end and starts before cutoff_start."""
    dur_td = timedelta(minutes=duration_minutes)
    end_dt = datetime.combine(_date.today(), cutoff_end)
    latest_by_end = (end_dt - dur_td).time()
    return min(latest_by_end, cutoff_start)


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


def _open_browser(p: object) -> tuple:
    browser, ctx, page = get_authenticated_context(p)  # type: ignore[arg-type]
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
def login() -> None:
    """Open browser and complete Feide/NTNU login. Saves the session."""
    _setup_logging(False)
    with sync_playwright() as p:
        try:
            browser, ctx, page = _open_browser(p)
            print("Login successful. Session stored in data/storage_state.json.")
            _close_browser(browser, ctx, page)
        except RuntimeError as e:
            print(f"Login failed: {e}", file=sys.stderr)
            raise typer.Exit(1)


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
                print(f"\nActive bookings ({len(bookings)}/8 quota used):")
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
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Show active bookings. Use --all or --count N to cancel."""
    _setup_logging(verbose)

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
        for slot_date, slot_start, _ in spec.all_slots():
            validate_lookahead(slot_date, slot_start)
    except RuleViolation as e:
        print(f"Rule violation: {e}", file=sys.stderr)
        raise typer.Exit(1)

    duration_minutes = int(duration_td.total_seconds() // 60)

    slots = spec.all_slots()
    total_bookings = sum(count for _, _, count in slots)

    cutoff_start_h, cutoff_start_m = (int(x) for x in spec.search_cutoff_start.split(":"))
    cutoff_end_h, cutoff_end_m = (int(x) for x in spec.search_cutoff_end.split(":"))
    cutoff_start = dtime(cutoff_start_h, cutoff_start_m)
    cutoff_end = dtime(cutoff_end_h, cutoff_end_m)
    latest_start = _latest_valid_start(duration_minutes, cutoff_start, cutoff_end)

    with sync_playwright() as p:
        browser, ctx, page = None, None, None
        booked_ids: list[str] = []

        try:
            browser, ctx, page = _open_browser(p)
            client = TPClient(page)

            print(f"\nBooking {total_bookings} room(s) across {len(slots)} slot(s) at {spec.campus}")

            any_success = False
            booking_num = 0
            failed = False
            booked_per_slot: dict[tuple[_date, str], set[str]] = defaultdict(set)

            for slot_date, slot_start, slot_count in slots:
                if failed:
                    break

                start_h, start_m = (int(x) for x in slot_start.split(":"))
                slot_start_time = dtime(start_h, start_m)
                max_search_date = slot_date + timedelta(days=spec.max_days_ahead)

                for i in range(slot_count):
                    booking_num += 1
                    booking_found = False
                    search_date = slot_date

                    while search_date <= max_search_date and not booking_found:
                        search_start = slot_start_time

                        while search_start <= latest_start and not booking_found:
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
                            print(f"[{booking_num}/{total_bookings}] Selected: {room.name}{type_tag} (cap {room.capacity}) on {search_date} at {search_start.strftime('%H:%M')}")

                            try:
                                result = client.confirm(room, criteria)
                            except QuotaExceededError:
                                print(f"[{booking_num}/{total_bookings}] Quota exceeded.", file=sys.stderr)
                                failed = True
                                break

                            if result.success:
                                print(f"[{booking_num}/{total_bookings}] Booked: {result.room.name} on {result.date} at {result.start}")
                                booked_per_slot[slot_key].add(room.name)
                                if result.booking_id:
                                    booked_ids.append(result.booking_id)
                                any_success = True
                                booking_found = True
                            else:
                                print(f"[{booking_num}/{total_bookings}] Warning: {result.message}")
                                search_start = _advance_time(search_start, spec.time_step_minutes)

                        if not booking_found and not failed:
                            search_date += timedelta(days=1)
                            if search_date <= max_search_date:
                                print(f"[{booking_num}/{total_bookings}] No room on {search_date - timedelta(days=1)}, trying {search_date}...")

                    if not booking_found and not failed:
                        print(f"[{booking_num}/{total_bookings}] No room found within search window.", file=sys.stderr)

                    if failed:
                        break

            client.flush_xhr_log(DEBUG_DIR)
            if any_success:
                refresh_session(ctx)

        except SessionExpiredError:
            if page:
                _save_debug(page, "session-expired")
            msg = "Session expired. Delete data/storage_state.json and run `koker login` again."
            print(msg, file=sys.stderr)
            raise typer.Exit(1)

        except QuotaExceededError:
            if page:
                _save_debug(page, "quota-exceeded")
            print("NTNU quota reached (max 8 bookings per 2-week period).", file=sys.stderr)
            raise typer.Exit(1)

        except NoRoomsAvailableError as e:
            if page:
                _save_debug(page, "no-rooms")
            print(f"No rooms available: {e}", file=sys.stderr)
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
            raise typer.Exit(1)

        except Exception as e:
            if page:
                _save_debug(page, "unexpected-error")
            logging.exception("Unexpected error")
            print(f"Unexpected error: {e}", file=sys.stderr)
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
