from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.sync_api import Locator, Page

from koker.models import BookingResult, MyBooking, Room, SearchCriteria

logger = logging.getLogger(__name__)

TARGET_URL = "https://tp.educloud.no/ntnu/app/rombestilling/"
MY_BOOKINGS_URL = "https://tp.educloud.no/ntnu/app/my-bookings"

QUOTA_PHRASES = [
    "quota",
    "limit reached",
    "too many bookings",
    "nådd grensen",
    "for mange bestillinger",
    "maks antall",
]
NO_ROOMS_PHRASES = [
    "no results",
    "no rooms",
    "0 rooms",
    "ingen ledige",
    "ingen rom",
    "fant ingen",
]


class BookingError(Exception):
    pass


class QuotaExceededError(BookingError):
    pass


class NoRoomsAvailableError(BookingError):
    pass


class SessionExpiredError(BookingError):
    pass


class TPClient:
    def __init__(self, page: Page) -> None:
        self.page = page
        self._xhr_log: list[str] = []
        self._booking_page_loaded = False
        self._attach_xhr_listener()

    def _attach_xhr_listener(self) -> None:
        from playwright.sync_api import Response

        def on_response(response: Response) -> None:
            try:
                url = response.url
                method = response.request.method
                status = response.status
                resource_type = response.request.resource_type
                if "tp.educloud.no/ntnu" in url and resource_type in ("xhr", "fetch"):
                    entry = f"{method} {status} {url}"
                    if "reservation.php" in url:
                        try:
                            req_body = (response.request.post_data or "")[:300]
                            resp_body = response.text()[:300]
                            logger.debug("reservation.php REQ: %s", req_body)
                            logger.debug("reservation.php RESP: %s", resp_body)
                            entry += f" | req={req_body!r} resp={resp_body!r}"
                        except Exception:
                            pass
                    if "possible.php" in url:
                        try:
                            resp_body = response.text()[:2000]
                            logger.debug("possible.php RESP: %s", resp_body)
                        except Exception:
                            pass
                    self._xhr_log.append(entry)
                    logger.debug("XHR: %s %s %s", method, status, url)
            except Exception:
                pass

        self.page.on("response", on_response)

    def flush_xhr_log(self, debug_dir: Path) -> None:
        if not self._xhr_log:
            return
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"xhr-{ts}.log").write_text("\n".join(self._xhr_log))

    def _is_login_page(self) -> bool:
        url = self.page.url
        return (
            "idp.feide.no" in url
            or "login.microsoftonline.com" in url
            or "auth.dataporten.no" in url
        )

    def _try_auto_reauth(self) -> bool:
        """Click NTNU on the Feide org-selection page and wait for SSO redirect back to TP.

        Works silently when Feide/NTNU/Microsoft session cookies are still valid.
        Returns True if we land back on tp.educloud.no within 90 s.
        """
        if "auth.dataporten.no" not in self.page.url:
            return False
        try:
            self.page.evaluate("""() => {
                const inp = document.getElementById('org_selector');
                if (inp) inp.value = 'feide|realm|ntnu.no';
                const form = document.getElementById('discoform_feide');
                if (form) form.submit();
            }""")
            return self._wait_for_tp_through_feide_chain()
        except Exception as e:
            logger.debug("Auto re-auth failed: %s", e)
            return False

    def _wait_for_tp_through_feide_chain(self, timeout_ms: int = 90_000) -> bool:
        """Poll until TP is reached, auto-clicking through intermediate Feide/NTNU pages."""
        step_ms = 2_000
        elapsed = 0
        while elapsed < timeout_ms:
            self.page.wait_for_timeout(step_ms)
            elapsed += step_ms
            url = self.page.url
            if "tp.educloud.no" in url and not self._is_login_page():
                self.page.wait_for_timeout(3000)
                return True
            if "idp.feide.no" in url or "feide.no/simplesaml" in url:
                logger.debug("NTNU auth method page - clicking Microsoft")
                try:
                    btn = self.page.get_by_text("Use work or school account", exact=False)
                    if btn.count() > 0:
                        btn.first.click()
                except Exception:
                    pass
        logger.debug("SSO chain timed out (URL: %s)", self.page.url)
        return False

    def _ensure_on_booking_page(self) -> None:
        """Navigate to the booking page only if not already there and authenticated."""
        if (
            self._booking_page_loaded
            and "tp.educloud.no/ntnu/app/rombestilling" in self.page.url
            and not self._is_login_page()
        ):
            return
        self.goto_booking()
        self._booking_page_loaded = True

    def goto_booking(self) -> None:
        # Always navigate fresh so the SPA initialises from a clean state
        self.page.goto(TARGET_URL, wait_until="domcontentloaded")
        # Give the SPA time to fire its initial XHRs and any auth redirects
        self.page.wait_for_timeout(5000)

        # Handle Feide org-selection redirect (SPA redirects here when API calls 401)
        if self._is_login_page():
            logger.info("SPA redirected to Feide; trying SSO re-auth")
            if not self._try_auto_reauth():
                raise SessionExpiredError(
                    "SSO re-auth failed. Run `koker login` to refresh the session."
                )
            self.page.wait_for_timeout(4000)

        if self._is_login_page():
            raise SessionExpiredError("Still on login page after re-auth. Run `koker login`.")

        if "tp.educloud.no/ntnu/app/rombestilling" not in self.page.url:
            raise SessionExpiredError("Not on TP booking page after navigation")

        try:
            self.page.wait_for_selector("input, [role='combobox'], button", timeout=15_000)
        except Exception as e:
            if self._is_login_page():
                raise SessionExpiredError("Session expired during form load") from e
            raise BookingError("Booking page did not load interactive elements") from e

        if self._is_login_page():
            raise SessionExpiredError("Session expired after SPA form load")

    def _select_combobox_option(self, combobox: Locator, label: str) -> None:
        try:
            combobox.click()
        except Exception:
            pass

        appeared = False
        try:
            self.page.wait_for_selector('[role="listbox"]', timeout=3_000)
            appeared = True
        except Exception:
            pass

        if not appeared:
            try:
                combobox.locator("xpath=following-sibling::button[1]").click()
                self.page.wait_for_selector('[role="listbox"]', timeout=3_000)
                appeared = True
            except Exception:
                pass

        if not appeared:
            logger.warning("Combobox listbox did not appear for '%s'", label)
            return

        option = self.page.get_by_role("option", name=label, exact=False)
        if option.count() == 0:
            option = self.page.locator('[role="option"]').filter(has_text=label)

        if option.count() > 0:
            option.first.click()
        else:
            self.page.keyboard.press("Escape")
            logger.warning("Option '%s' not found in combobox", label)

    def _fetch_possible(self, criteria: SearchCriteria) -> list[Any]:
        self._ensure_on_booking_page()

        start_dt = datetime.combine(criteria.date, criteria.start)
        end_dt = start_dt + timedelta(minutes=criteria.duration_minutes)
        start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
        duration_ms = criteria.duration_minutes * 60 * 1000

        logger.info(
            "Searching: date=%s start=%s end=%s area=%s",
            criteria.date.isoformat(), start_iso, end_iso, criteria.area_label,
        )

        possible_url = (
            f"https://tp.educloud.no/ntnu/ws/rombestilling/possible.php"
            f"?start={start_iso}&duration={duration_ms}&end={end_iso}"
        )

        try:
            rooms_data: Any = self.page.evaluate(
                f"""async () => {{
                    const resp = await fetch({possible_url!r}, {{credentials: "include"}});
                    if (!resp.ok) return [];
                    return await resp.json();
                }}"""
            )
        except Exception as e:
            logger.warning("possible.php fetch failed: %s", e)
            rooms_data = []

        logger.debug("possible.php returned %d rooms total", len(rooms_data) if rooms_data else 0)
        return rooms_data or []

    def search(self, criteria: SearchCriteria) -> list[Room]:
        return self._parse_api_rooms(self._fetch_possible(criteria), criteria)

    def search_full(self, criteria: SearchCriteria) -> list[Room]:
        """Return rooms matching criteria filters that are full/unbookable at this time."""
        return self._parse_api_rooms(self._fetch_possible(criteria), criteria, unavailable=True)

    def _parse_api_rooms(self, rooms_data: list[Any], criteria: SearchCriteria, unavailable: bool = False) -> list[Room]:
        rooms: list[Room] = []
        area_label_lower = (criteria.area_label or "").lower()

        for item in rooms_data:
            is_available = bool(item.get("available") and item.get("can_book"))
            if unavailable and is_available:
                continue
            if not unavailable and not is_available:
                continue

            if area_label_lower:
                area_name = (item.get("area_name") or "").lower()
                building_name = (item.get("building_name") or "").lower()
                if area_label_lower not in area_name and area_label_lower not in building_name:
                    continue

            if criteria.building_label:
                building_name = (item.get("building_name") or "").lower()
                if criteria.building_label.lower() not in building_name:
                    continue

            if criteria.excluded_buildings:
                building_name = (item.get("building_name") or "").lower()
                if any(ex.lower() in building_name for ex in criteria.excluded_buildings):
                    continue

            size = int(item.get("size") or 0)
            # size=0 means TP doesn't report capacity for this room; include it anyway
            if size > 0 and size < criteria.min_capacity:
                continue

            roomtype_obj = item.get("roomtype") or {}
            room_type_name = (
                roomtype_obj.get("name_en")
                or roomtype_obj.get("name")
                or ""
            )
            if criteria.room_type_label:
                if criteria.room_type_label.lower() not in room_type_name.lower():
                    continue

            room_id = str(item.get("id") or "")
            room_name = str(item.get("name") or item.get("acronym") or "")
            building = str(item.get("building_name") or "")

            if room_name and room_id:
                logger.debug(
                    "Available: id=%s name='%s' building='%s' type='%s' cap=%d area='%s'",
                    room_id, room_name, building, room_type_name, size,
                    item.get("area_name", ""),
                )
                rooms.append(Room(
                    id=room_id,
                    name=room_name,
                    capacity=size,
                    building=building,
                    room_type=room_type_name,
                ))

        # When preferred rooms are set, log their status in the raw API response
        if criteria.preferred_rooms and rooms_data:
            area_lower = (criteria.area_label or "").lower()
            for item in rooms_data:
                item_name = str(item.get("name") or item.get("acronym") or "")
                item_id = str(item.get("id") or "")
                for pref in criteria.preferred_rooms:
                    if pref.lower() in item_name.lower() or pref.lower() in item_id.lower():
                        in_area = area_lower in (item.get("area_name") or "").lower()
                        logger.info(
                            "Preferred '%s': found as '%s' (id=%s) area='%s' available=%s can_book=%s cap=%s",
                            pref, item_name, item_id,
                            item.get("area_name", ""),
                            item.get("available"), item.get("can_book"), item.get("size"),
                        )
                        if not in_area:
                            logger.info("  ^ filtered out: not in area '%s'", criteria.area_label)

        if not rooms:
            available_count = sum(
                1 for r in rooms_data if r.get("available") and r.get("can_book")
            )
            glosh_count = sum(
                1 for r in rooms_data
                if "gløshaugen" in (r.get("area_name") or "").lower()
            )
            logger.warning(
                "No matching rooms. Total from API: %d, available+bookable: %d, "
                "in Gløshaugen area: %d, area_filter='%s', min_cap=%d",
                len(rooms_data), available_count, glosh_count,
                criteria.area_label, criteria.min_capacity,
            )

        return rooms

    def pick_room(self, rooms: list[Room], criteria: SearchCriteria) -> Room | None:
        if not rooms:
            return None

        # Filter excluded rooms
        rooms = [
            r for r in rooms
            if not any(ex.lower() in r.name.lower() or ex.lower() in r.id.lower() for ex in criteria.excluded_rooms)
        ]
        if not rooms:
            logger.debug("All rooms filtered by excluded_rooms=%s", criteria.excluded_rooms)
            return None

        # Preferred room names first (skip when shuffling)
        if not criteria.shuffle_rooms:
            for pref in criteria.preferred_rooms:
                for room in rooms:
                    if pref.lower() in room.id.lower() or pref.lower() in room.name.lower():
                        return room
            if criteria.preferred_rooms:
                logger.info(
                    "Preferred rooms %s not found among %d available: %s",
                    criteria.preferred_rooms, len(rooms), [r.name for r in rooms[:10]],
                )

        eligible = [r for r in rooms if r.capacity >= criteria.min_capacity]
        if not eligible:
            eligible = rooms

        if criteria.shuffle_rooms:
            import random
            random.shuffle(eligible)
            return eligible[0]

        # Sort: preferred buildings first, then preferred room types, then ascending capacity
        def sort_key(r: Room) -> tuple[int, int, int]:
            bld_rank = len(criteria.preferred_buildings)
            for i, pb in enumerate(criteria.preferred_buildings):
                if pb.lower() in r.building.lower():
                    bld_rank = i
                    break
            type_rank = len(criteria.preferred_room_types)
            for i, pt in enumerate(criteria.preferred_room_types):
                if pt.lower() in r.room_type.lower():
                    type_rank = i
                    break
            return (bld_rank, type_rank, r.capacity)

        eligible.sort(key=sort_key)
        return eligible[0]

    def confirm(self, room: Room, criteria: SearchCriteria) -> BookingResult:
        import json

        logger.info("Confirming booking: room='%s' id=%s", room.name, room.id)

        start_dt = datetime.combine(criteria.date, criteria.start)
        end_dt = start_dt + timedelta(minutes=criteria.duration_minutes)
        start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%S")
        end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%S")

        payload = {
            "rooms": [room.id],
            "userGroup": None,
            "name": criteria.description,
            "notes": criteria.notes or "",
            "start": start_iso,
            "end": end_iso,
        }
        logger.debug("reservation.php POST: %s", json.dumps(payload))

        result: Any = self.page.evaluate(
            """async (payload) => {
                try {
                    const resp = await fetch(
                        'https://tp.educloud.no/ntnu/ws/rombestilling/reservation.php',
                        {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                                'Content-Type': 'application/json',
                                'X-Requested-With': 'XMLHttpRequest',
                            },
                            body: JSON.stringify(payload),
                        }
                    );
                    const text = await resp.text();
                    return {status: resp.status, body: text};
                } catch (e) {
                    return {status: 0, body: String(e)};
                }
            }""",
            payload,
        )

        success = False
        booking_id = ""
        message = "Unknown error"

        if result and result.get("status") == 200:
            try:
                body = json.loads(result["body"])
                completed = body.get("reservations-completed", [])
                if completed:
                    entry = completed[0]
                    if entry.get("ok"):
                        success = True
                        booking_id = str(entry.get("id") or "")
                        message = str(entry.get("message") or "Booked")
                    else:
                        message = str(entry.get("message") or "Booking rejected by server")
                        if "quota" in message.lower() or "kvote" in message.lower():
                            raise QuotaExceededError(message)
                else:
                    message = result["body"][:300]
            except QuotaExceededError:
                raise
            except Exception as e:
                logger.warning(
                    "Failed to parse reservation response: %s | body: %s",
                    e, result.get("body", "")[:300],
                )
                message = result.get("body", "")[:300]
        else:
            status = result.get("status") if result else "no response"
            body_preview = (result.get("body") or "")[:300] if result else ""
            message = f"HTTP {status}: {body_preview}"
            logger.warning("reservation.php returned: %s", message)

        logger.debug("confirm result: success=%s id=%s msg=%s", success, booking_id, message)

        return BookingResult(
            success=success,
            room=room,
            date=criteria.date,
            start=criteria.start,
            duration_minutes=criteria.duration_minutes,
            booking_id=booking_id,
            message=message,
        )

    def _parse_captured_bookings(self, data: list[Any]) -> list[MyBooking]:
        bookings: list[MyBooking] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            # bookings.php: top-level id is the reservation ID
            booking_id = str(
                item.get("id")
                or item.get("reservationid")
                or item.get("reservation_id")
                or item.get("bid")
                or ""
            )
            # bookings.php uses 'rooms' (array); legacy used 'room' (dict)
            rooms_list = item.get("rooms")
            room_info = item.get("room") or {}
            if isinstance(rooms_list, list) and rooms_list:
                first = rooms_list[0] if isinstance(rooms_list[0], dict) else {}
                room_name = str(first.get("name") or first.get("acronym") or "")
            elif isinstance(room_info, dict):
                room_name = str(room_info.get("name") or room_info.get("acronym") or "")
            else:
                room_name = str(item.get("room_name") or "")
            # bookings.php: 'booked' = "2026-05-11 09:00:00", 'tid' = "09:00-11:00", 'firstday' = "2026-05-11"
            # legacy: 'start' / 'starttime' = ISO datetime string
            date_str = ""
            time_str = ""
            booked_str = str(item.get("booked") or item.get("start") or item.get("starttime") or "")
            if booked_str:
                try:
                    dt = datetime.fromisoformat(booked_str.replace("Z", "+00:00"))
                    date_str = dt.strftime("%d.%m.%Y")
                    time_str = dt.strftime("%H:%M")
                except Exception:
                    pass
            if not date_str:
                firstday = str(item.get("firstday") or item.get("preset_date") or "")
                if firstday:
                    try:
                        date_str = datetime.strptime(firstday, "%Y-%m-%d").strftime("%d.%m.%Y")
                    except Exception:
                        pass
            tid = str(item.get("tid") or item.get("preset_start") or "")
            end_str = ""
            if tid and "-" in tid:
                parts = tid.split("-")
                end_str = parts[1].strip()[:5]
            if not time_str and tid:
                time_str = tid.split("-")[0].strip()[:5]
            logger.debug("parsed booking: id=%r room=%r date=%s", booking_id, room_name, date_str)
            if booking_id or room_name:
                bookings.append(MyBooking(
                    booking_id=booking_id,
                    room_name=room_name,
                    date_str=date_str,
                    time_str=time_str,
                    end_str=end_str,
                ))
        return bookings

    def cancel_booking_api(self, booking_id: str) -> bool:
        """Cancel a specific booking by ID via DELETE API. ID goes as query param."""
        result: Any = self.page.evaluate(
            """async (id) => {
                try {
                    const url = 'https://tp.educloud.no/ntnu/ws/rombestilling/reservation.php?id=' + encodeURIComponent(id);
                    const resp = await fetch(url, {
                        method: 'DELETE',
                        credentials: 'include',
                        headers: {'Accept': 'application/json, text/plain, */*'},
                    });
                    const text = await resp.text();
                    return {status: resp.status, body: text};
                } catch(e) {
                    return {status: 0, body: String(e)};
                }
            }""",
            booking_id,
        )

        if not result:
            return False

        status = result.get("status", 0)
        body = result.get("body", "")
        logger.debug("cancel_booking_api %s -> %d: %s", booking_id, status, body[:200])
        return status in (200, 204)

    def cancel_n_bookings(self, booking_ids: list[str]) -> int:
        """Cancel specific bookings by ID via the reservation API."""
        if "tp.educloud.no/ntnu" not in self.page.url or self._is_login_page():
            self.page.goto(MY_BOOKINGS_URL, wait_until="domcontentloaded")
            if self._is_login_page():
                raise SessionExpiredError("Session expired")
            self.page.wait_for_timeout(2000)

        cancelled = 0
        for bid in booking_ids:
            if not bid:
                continue
            if self.cancel_booking_api(bid):
                logger.info("Cancelled %s via API", bid)
                cancelled += 1
            else:
                logger.warning("API cancel failed for %s", bid)
        return cancelled

    def get_my_bookings(self) -> list[MyBooking]:
        """Navigate to my-bookings SPA and intercept bookings.php."""
        captured: list[Any] = []

        def _capture(response: Any) -> None:
            try:
                url = response.url
                if "bookings.php" not in url:
                    return
                if response.request.resource_type not in ("xhr", "fetch"):
                    return
                if not response.ok:
                    return
                ct = response.headers.get("content-type") or ""
                if "json" not in ct:
                    return
                data = response.json()
                logger.debug("bookings.php: %d items", len(data) if isinstance(data, list) else -1)
                if isinstance(data, list):
                    if data:
                        import json as _json
                        logger.debug("bookings.php first item: %s", _json.dumps(data[0])[:2000])
                    captured.extend(data)
            except Exception as exc:
                logger.debug("Booking intercept error: %s", exc)

        self.page.on("response", _capture)
        try:
            self.page.goto(MY_BOOKINGS_URL, wait_until="domcontentloaded")
            if self._is_login_page():
                raise SessionExpiredError("Session expired")
            self.page.wait_for_timeout(5000)
        finally:
            self.page.remove_listener("response", _capture)

        if captured:
            logger.debug("Intercepted %d booking entries from bookings.php", len(captured))
            return self._parse_captured_bookings(captured)

        logger.debug("No bookings.php response captured; trying DOM scrape")
        return self._scrape_my_bookings_dom()

    def _scrape_my_bookings_dom(self) -> list[MyBooking]:
        """Scrape booking list from the current DOM on the my-bookings page."""
        import re

        data: Any = self.page.evaluate(
            r"""() => {
                const results = [];
                // Try table rows
                const rows = document.querySelectorAll('tr');
                for (const row of rows) {
                    const cells = row.querySelectorAll('td');
                    if (cells.length < 2) continue;
                    const texts = Array.from(cells).map(c => (c.innerText || '').trim()).filter(Boolean);
                    if (!texts.length) continue;
                    // Look for booking ID in data attrs or links
                    let bid = row.getAttribute('data-id') || row.getAttribute('data-booking-id') || '';
                    if (!bid) {
                        const link = row.querySelector('a');
                        if (link) {
                            const m = link.href.match(/[?&]id=([^&]+)/) || link.href.match(/\/([A-Za-z0-9_-]{5,})\/?$/);
                            if (m) bid = m[1];
                        }
                    }
                    results.push({booking_id: bid, texts: texts, raw: row.innerText.trim()});
                }
                if (results.length) return results;
                // Fallback: find any element with booking-like class
                const cards = document.querySelectorAll('[class*="booking" i], [class*="reservation" i], [class*="bestilling" i]');
                for (const card of cards) {
                    const raw = (card.innerText || '').trim();
                    if (raw) results.push({booking_id: '', texts: [], raw: raw});
                }
                return results;
            }"""
        )

        bookings: list[MyBooking] = []
        for item in (data or []):
            texts = item.get("texts", [])
            raw = item.get("raw", "")
            booking_id = item.get("booking_id", "")
            if not raw and not texts:
                continue
            date_str = ""
            time_str = ""
            end_str = ""
            room_name = ""
            for t in texts:
                if re.match(r"\d{2}\.\d{2}\.\d{4}", t):
                    date_str = t
                else:
                    range_m = re.match(r"(\d{2}:\d{2})-(\d{2}:\d{2})", t)
                    if range_m:
                        if not time_str:
                            time_str = range_m.group(1)
                        end_str = range_m.group(2)
                    elif re.match(r"\d{2}:\d{2}", t) and not time_str:
                        time_str = t
                    elif t and not room_name and len(t) > 1:
                        room_name = t
            if not room_name and raw:
                room_name = raw.split("\n")[0].strip()
            if room_name or booking_id:
                bookings.append(MyBooking(
                    booking_id=booking_id,
                    room_name=room_name,
                    date_str=date_str,
                    time_str=time_str,
                    end_str=end_str,
                    raw=raw,
                ))
        return bookings

    def cancel_all_bookings(self, limit: int | None = None) -> int:
        """Cancel all (or up to `limit`) bookings."""
        bookings = self.get_my_bookings()
        ids = [b.booking_id for b in bookings if b.booking_id]
        if limit is not None:
            ids = ids[:limit]
        if ids:
            return self.cancel_n_bookings(ids)
        logger.warning(
            "cancel_all_bookings: no booking IDs found (got %d entries without IDs)",
            len(bookings),
        )
        return 0
