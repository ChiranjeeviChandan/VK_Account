"""Calendars: one merged view over Felix's own calendar, ICS feeds, Google
Calendar and the calendars synced on the owner's Android phone.

Events are cached locally, so the agenda, conflict checks and free-slot
search work offline. Felix's own calendar is always writable; Google and
phone calendars are written through when reachable.
"""

from __future__ import annotations

import logging
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from ..timeparse import format_when, parse_datetime_loose, parse_range, parse_when
from ..tools import LOCAL, OUTBOUND, READ, Registry, ToolContext, ToolError
from ..util import clip, fold, parse_hhmm, resolve_tz
from . import ics
from .rrule import RuleError, describe, expand, parse_rrule, repeat_to_rrule

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix

log = logging.getLogger("felix.calendar")

SYNC_BACK_DAYS = 7
SYNC_AHEAD_DAYS = 120
SOURCE_RANK = {"google": 0, "phone": 1, "local": 2, "ics": 3}


class Calendar:
    def __init__(self, app: "Felix") -> None:
        self.app = app
        self._sync_lock = threading.Lock()
        from .calendar_google import GoogleCalendar

        self.google = GoogleCalendar(app, self)

    # -- sources ---------------------------------------------------------------
    def sources(self) -> list[dict[str, Any]]:
        sources = list(self.app.config.get("calendar.sources", []) or [])
        if not any(s.get("kind") == "local" for s in sources):
            sources.insert(0, {"id": "local", "kind": "local", "name": "Felix"})
        return sources

    def source(self, source_id: str | None) -> dict[str, Any]:
        sources = self.sources()
        wanted = source_id or self.app.config.get("calendar.default_source", "local")
        for source in sources:
            if source["id"] == wanted or fold(source.get("name", "")) == fold(wanted):
                return source
        if source_id:
            raise ToolError(f"There is no calendar called {source_id!r}. Known: {', '.join(s['id'] for s in sources)}")
        return sources[0]

    def add_source(self, source: dict[str, Any]) -> dict[str, Any]:
        if source.get("kind") not in ("local", "ics", "google", "phone"):
            raise ToolError("A calendar source must be local, ics, google or phone.")
        source.setdefault("id", f"{source['kind']}-{uuid.uuid4().hex[:6]}")
        source.setdefault("name", source["id"])
        sources = [s for s in self.sources() if s["id"] != source["id"]] + [source]
        self.app.config.set("calendar.sources", sources)
        return source

    def remove_source(self, source_id: str) -> None:
        sources = [s for s in self.sources() if s["id"] != source_id]
        self.app.config.set("calendar.sources", sources)
        for row in self.app.store.query("SELECT id FROM events WHERE source = ?", (source_id,)):
            self.app.store.unindex("event", row["id"])
        self.app.store.delete("events", "source = ?", (source_id,))

    def writable(self, source: dict[str, Any]) -> bool:
        return source["kind"] in ("local", "google", "phone")

    def tz(self, name: str | None = None):
        return resolve_tz(name) if name else self.app.clock.tz

    # -- reading ---------------------------------------------------------------
    def between(self, start: datetime, end: datetime, include_declined: bool = False,
                query: str | None = None) -> list[dict[str, Any]]:
        """Every occurrence that overlaps [start, end), merged across sources."""
        start_ts, end_ts = start.timestamp(), end.timestamp()
        active = {s["id"]: s for s in self.sources()}
        occurrences: list[dict[str, Any]] = []

        singles = self.app.store.query(
            "SELECT * FROM events WHERE (rrule IS NULL OR rrule = '') AND start_ts < ? AND end_ts > ? "
            "AND status != 'cancelled'", (end_ts, start_ts))
        for row in singles:
            if row["source"] in active:
                occurrences.append(self._occurrence(row, row["start_ts"], row["end_ts"], active[row["source"]]))

        masters = self.app.store.query(
            "SELECT * FROM events WHERE rrule IS NOT NULL AND rrule != '' AND start_ts < ? AND status != 'cancelled'",
            (end_ts,))
        for row in masters:
            if row["source"] not in active:
                continue
            duration = row["end_ts"] - row["start_ts"]
            zone = self.tz(row["tz"])
            first = datetime.fromtimestamp(row["start_ts"], zone)
            try:
                rule = parse_rrule(row["rrule"], zone)
            except RuleError:
                continue
            overridden = {r["recurrence_id"] for r in self.app.store.query(
                "SELECT recurrence_id FROM events WHERE source = ? AND uid = ? AND recurrence_id IS NOT NULL",
                (row["source"], row["uid"]))}
            skipped = {datetime.fromtimestamp(ts, zone) for ts in (row["exdates"] or [])}
            window_start = datetime.fromtimestamp(start_ts - duration, zone)
            for moment in expand(first, rule, window_start, datetime.fromtimestamp(end_ts, zone), skipped):
                ts = moment.timestamp()
                if ts in overridden or ts + duration <= start_ts:
                    continue
                occurrences.append(self._occurrence(row, ts, ts + duration, active[row["source"]], recurring=True))

        if not include_declined:
            occurrences = [o for o in occurrences if o["my_response"] != "declined"]
        if query:
            words = fold(query).split()
            occurrences = [o for o in occurrences
                           if all(w in fold(f"{o['title']} {o['location']} {' '.join(a.get('name') or a.get('email', '') for a in o['attendees'])}")
                                  for w in words)]
        # The same meeting often arrives through two sources (Google API and the phone).
        best: dict[tuple, dict[str, Any]] = {}
        for item in occurrences:
            key = (fold(item["title"]), round(item["start_ts"]), round(item["end_ts"]))
            if key not in best or SOURCE_RANK[item["source_kind"]] < SOURCE_RANK[best[key]["source_kind"]]:
                best[key] = item
        return sorted(best.values(), key=lambda o: (o["start_ts"], o["end_ts"], o["title"]))

    def _occurrence(self, row: dict[str, Any], start_ts: float, end_ts: float, source: dict[str, Any],
                    recurring: bool = False) -> dict[str, Any]:
        is_series = recurring or bool(row["series_id"]) or row["recurrence_id"] is not None
        return {
            "id": f"{row['id']}:{int(start_ts)}" if recurring else str(row["id"]),
            "row_id": row["id"], "source": row["source"], "source_kind": source["kind"],
            "calendar": row["calendar"] or source.get("name") or row["source"],
            "title": row["title"] or "(untitled)", "start_ts": start_ts, "end_ts": end_ts,
            "start": self.app.clock.from_ts(start_ts), "end": self.app.clock.from_ts(end_ts),
            "all_day": bool(row["all_day"]), "location": row["location"] or "",
            "description": row["description"] or "", "attendees": row["attendees"] or [],
            "organizer": row["organizer"] or "", "my_response": row["my_response"] or "",
            "recurring": is_series, "repeat": describe(row["rrule"]) if row["rrule"] else "",
            "url": row["url"] or "", "writable": self.writable(source),
        }

    def get_row(self, row_id: int) -> dict[str, Any]:
        row = self.app.store.one("SELECT * FROM events WHERE id = ?", (row_id,))
        if row is None:
            raise ToolError(f"There is no calendar event #{row_id}.")
        return row

    def resolve(self, reference: str) -> dict[str, Any]:
        """Turn an event id, or a few words of a title, into one occurrence."""
        text = str(reference).strip().lstrip("#")
        now = self.app.clock.now()
        if text.replace(":", "").isdigit():
            row_id, _, stamp = text.partition(":")
            row = self.get_row(int(row_id))
            source = self.source(row["source"])
            if stamp:
                duration = row["end_ts"] - row["start_ts"]
                return self._occurrence(row, float(stamp), float(stamp) + duration, source, recurring=True)
            if row["rrule"]:
                upcoming = [o for o in self.between(now - timedelta(hours=1), now + timedelta(days=370))
                            if o["row_id"] == row["id"]]
                if upcoming:
                    return upcoming[0]
            return self._occurrence(row, row["start_ts"], row["end_ts"], source)
        when = parse_when(text, now, self.app.config.get("owner.date_order", "dmy"), prefer_future=False)
        words = (when.rest if when else text).strip()
        if when is not None and when.has_date:
            day = when.start.replace(hour=0, minute=0, second=0, microsecond=0)
            pool = self.between(day, day + timedelta(days=1))
            if when.has_time:
                pool = [o for o in pool if abs(o["start_ts"] - when.start.timestamp()) < 60] or pool
        elif when is not None and when.has_time:
            pool = [o for o in self.between(when.start - timedelta(minutes=1), when.start + timedelta(minutes=1))
                    if abs(o["start_ts"] - when.start.timestamp()) < 60]
        else:
            pool = self.between(now - timedelta(hours=2), now + timedelta(days=45))
        if words:
            tokens = fold(words).split()
            pool = [o for o in pool if all(t in fold(o["title"]) for t in tokens)]
        if not pool and when is not None:
            # Words such as "lunch" or "evening" read as a time of day but may simply be the title.
            tokens = fold(text).split()
            pool = [o for o in self.between(now - timedelta(hours=2), now + timedelta(days=45))
                    if all(t in fold(o["title"]) for t in tokens)]
        if not pool:
            raise ToolError(f"I can't find an event matching {reference!r}.")
        titles = {fold(o["title"]) for o in pool}
        if len(titles) > 1:
            listing = "; ".join(f"#{o['id']} {o['title']} ({self.when(o)})" for o in pool[:6])
            raise ToolError(f"More than one event matches {reference!r}: {listing}")
        upcoming = [o for o in pool if o["end_ts"] >= now.timestamp()]
        return (upcoming or pool)[0]

    def when(self, occurrence: dict[str, Any]) -> str:
        end = occurrence["end"]
        if occurrence["all_day"]:
            end = None
        return format_when(occurrence["start"], end, self.app.clock.now(), all_day=occurrence["all_day"])

    def public(self, occurrence: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": occurrence["id"], "title": occurrence["title"], "when": self.when(occurrence),
            "start": occurrence["start"].isoformat(timespec="minutes"),
            "end": occurrence["end"].isoformat(timespec="minutes"),
        }
        if occurrence["all_day"]:
            out["all_day"] = True
        if occurrence["location"]:
            out["location"] = occurrence["location"]
        if occurrence["attendees"]:
            out["attendees"] = [a.get("name") or a.get("email") for a in occurrence["attendees"]][:12]
        if occurrence["repeat"]:
            out["repeats"] = occurrence["repeat"]
        elif occurrence["recurring"]:
            out["repeats"] = "part of a series"
        if occurrence["my_response"] in ("pending", "needsaction", "tentative"):
            out["awaiting_your_rsvp"] = True
        if len(self.sources()) > 1:
            out["calendar"] = occurrence["calendar"]
        if occurrence["description"]:
            out["notes"] = clip(occurrence["description"], 300)
        return out

    # -- availability ------------------------------------------------------------
    def busy(self, start: datetime, end: datetime, ignore_row: int | None = None) -> list[tuple[float, float]]:
        spans = sorted((o["start_ts"], o["end_ts"]) for o in self.between(start, end)
                       if not o["all_day"] and o["row_id"] != ignore_row)
        merged: list[tuple[float, float]] = []
        for begin, finish in spans:
            if merged and begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
            else:
                merged.append((begin, finish))
        return merged

    def conflicts(self, start: datetime, end: datetime, ignore_row: int | None = None) -> list[dict[str, Any]]:
        return [o for o in self.between(start, end)
                if not o["all_day"] and o["row_id"] != ignore_row
                and o["start_ts"] < end.timestamp() and o["end_ts"] > start.timestamp()]

    def free_slots(self, start: datetime, end: datetime, minutes: int, limit: int = 12) -> list[tuple[datetime, datetime]]:
        """Gaps of at least ``minutes`` inside working hours between start and end."""
        hours = self.app.config.get("calendar.work_hours", {}) or {}
        open_at = parse_hhmm(hours.get("start", "09:00"), (9, 0))
        close_at = parse_hhmm(hours.get("end", "18:00"), (18, 0))
        workdays = set(hours.get("days", [0, 1, 2, 3, 4]))
        need = minutes * 60
        slots: list[tuple[datetime, datetime]] = []
        day = start.astimezone(self.app.clock.tz).date()
        last_day = end.astimezone(self.app.clock.tz).date()
        while day <= last_day and len(slots) < limit:
            if day.weekday() in workdays:
                window_start = max(datetime.combine(day, time(*open_at), tzinfo=self.app.clock.tz), start)
                window_end = min(datetime.combine(day, time(*close_at), tzinfo=self.app.clock.tz), end)
                cursor = window_start.timestamp()
                for begin, finish in self.busy(window_start, window_end) + [(window_end.timestamp(), window_end.timestamp())]:
                    if begin - cursor >= need and len(slots) < limit:
                        slots.append((self.app.clock.from_ts(cursor), self.app.clock.from_ts(begin)))
                    cursor = max(cursor, finish)
            day += timedelta(days=1)
        return slots

    def next_event(self, after: datetime | None = None) -> dict[str, Any] | None:
        now = after or self.app.clock.now()
        for occurrence in self.between(now, now + timedelta(days=14)):
            if not occurrence["all_day"] and occurrence["start_ts"] >= now.timestamp():
                return occurrence
        return None

    def day(self, moment: datetime) -> list[dict[str, Any]]:
        start = moment.astimezone(self.app.clock.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        return self.between(start, start + timedelta(days=1))

    # -- writing -----------------------------------------------------------------
    def create(self, title: str, start: datetime, end: datetime, all_day: bool = False, location: str = "",
               description: str = "", attendees: list[dict[str, str]] | None = None, rrule: str | None = None,
               reminders: list[int] | None = None, source_id: str | None = None) -> dict[str, Any]:
        source = self.source(source_id)
        if not self.writable(source):
            raise ToolError(f"The calendar {source['name']!r} is read-only; choose another one.")
        if end <= start:
            raise ToolError("An event must end after it starts.")
        fields = {
            "title": title.strip(), "start": start, "end": end, "all_day": all_day, "location": location,
            "description": description, "attendees": attendees or [], "rrule": rrule or "",
            "reminders": reminders or [],
        }
        if source["kind"] == "google":
            row_id = self.google.create(source, fields)
        elif source["kind"] == "phone":
            row_id = self._phone_create(source, fields)
        else:
            row_id = self._insert(source["id"], {
                "remote_id": None, "uid": f"{uuid.uuid4()}@felix", "title": fields["title"],
                "description": description, "location": location, "start_ts": start.timestamp(),
                "end_ts": end.timestamp(), "all_day": int(all_day), "tz": getattr(start.tzinfo, "key", "") or "",
                "rrule": rrule or "", "attendees": attendees or [], "reminders": reminders or [],
                "status": "confirmed", "my_response": "accepted",
            })
        self.app.bus.publish("calendar.changed", {"id": row_id})
        row = self.get_row(row_id)
        return self._occurrence(row, row["start_ts"], row["end_ts"], source)

    def update(self, occurrence: dict[str, Any], changes: dict[str, Any], whole_series: bool = False) -> dict[str, Any]:
        row = self.get_row(occurrence["row_id"])
        source = self.source(row["source"])
        if not self.writable(source):
            raise ToolError(f"{row['title']!r} is on a read-only calendar and cannot be changed from Felix.")
        if source["kind"] == "google":
            row_id = self.google.update(source, row, changes, whole_series)
        elif source["kind"] == "phone":
            row_id = self._phone_update(source, row, changes)
        elif row["rrule"] and not whole_series:
            row_id = self._override(row, occurrence, changes)
        else:
            values = self._column_values(changes)
            if row["rrule"] and "start_ts" in values:
                # Moving a whole series keeps each occurrence's length.
                values.setdefault("end_ts", values["start_ts"] + (row["end_ts"] - row["start_ts"]))
            self.app.store.update("events", {**values, "updated_at": self.app.clock.ts()}, "id = ?", (row["id"],))
            row_id = row["id"]
            self._index(row_id)
        self.app.bus.publish("calendar.changed", {"id": row_id})
        fresh = self.get_row(row_id)
        if fresh["rrule"]:
            return self.resolve(str(row_id))
        return self._occurrence(fresh, fresh["start_ts"], fresh["end_ts"], source)

    def delete(self, occurrence: dict[str, Any], whole_series: bool = False) -> None:
        row = self.get_row(occurrence["row_id"])
        source = self.source(row["source"])
        if not self.writable(source):
            raise ToolError(f"{row['title']!r} is on a read-only calendar and cannot be removed from Felix.")
        if source["kind"] == "google":
            self.google.delete(source, row, whole_series)
        elif source["kind"] == "phone":
            self._phone_delete(source, row)
        elif row["rrule"] and not whole_series:
            exdates = list(row["exdates"] or []) + [occurrence["start_ts"]]
            self.app.store.update("events", {"exdates": exdates}, "id = ?", (row["id"],))
        else:
            if row["rrule"]:
                for extra in self.app.store.query(
                        "SELECT id FROM events WHERE source = ? AND uid = ? AND id != ?",
                        (row["source"], row["uid"], row["id"])):
                    self._remove_row(extra["id"])
            self._remove_row(row["id"])
        self.app.bus.publish("calendar.changed", {"id": row["id"]})

    def _override(self, master: dict[str, Any], occurrence: dict[str, Any], changes: dict[str, Any]) -> int:
        """Change one occurrence of a local series by storing an exception."""
        values = {
            "remote_id": None, "uid": master["uid"], "title": master["title"], "description": master["description"],
            "location": master["location"], "start_ts": occurrence["start_ts"], "end_ts": occurrence["end_ts"],
            "all_day": master["all_day"], "tz": master["tz"], "rrule": "", "attendees": master["attendees"],
            "reminders": master["reminders"], "status": "confirmed", "my_response": master["my_response"],
            "recurrence_id": occurrence["start_ts"],
        }
        values.update(self._column_values(changes))
        return self._insert(master["source"], values)

    def _column_values(self, changes: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for key in ("title", "location", "description", "attendees", "reminders", "rrule"):
            if key in changes and changes[key] is not None:
                values[key] = changes[key]
        if changes.get("start") is not None:
            values["start_ts"] = changes["start"].timestamp()
        if changes.get("end") is not None:
            values["end_ts"] = changes["end"].timestamp()
        if changes.get("all_day") is not None:
            values["all_day"] = int(changes["all_day"])
        return values

    def _insert(self, source_id: str, values: dict[str, Any]) -> int:
        values = {"exdates": [], "expanded": 0, "updated_at": self.app.clock.ts(), **values, "source": source_id}
        row_id = self.app.store.insert("events", values)
        self._index(row_id)
        return row_id

    def upsert_remote(self, source_id: str, remote_id: str, values: dict[str, Any]) -> int:
        """Insert or refresh a cached instance from Google or the phone."""
        existing = self.app.store.one("SELECT id FROM events WHERE source = ? AND remote_id = ?", (source_id, remote_id))
        values = {"exdates": [], "updated_at": self.app.clock.ts(), **values, "source": source_id,
                  "remote_id": remote_id, "expanded": 1}
        if existing:
            self.app.store.update("events", values, "id = ?", (existing["id"],))
            row_id = existing["id"]
        else:
            row_id = self.app.store.insert("events", values)
        self._index(row_id)
        return row_id

    def _index(self, row_id: int) -> None:
        row = self.app.store.one("SELECT * FROM events WHERE id = ?", (row_id,))
        if row:
            people = " ".join(f"{a.get('name', '')} {a.get('email', '')}" for a in row["attendees"] or [])
            self.app.store.index("event", row_id, row["title"] or "",
                                 f"{row['location'] or ''} {row['description'] or ''}", people, row["start_ts"])

    def _remove_row(self, row_id: int) -> None:
        self.app.store.delete("events", "id = ?", (row_id,))
        self.app.store.unindex("event", row_id)

    def prune(self, source_id: str, keep_remote_ids: set[str], start_ts: float, end_ts: float,
              calendar: str | None = None) -> int:
        """Drop cached instances in a synced window that the provider no longer has."""
        sql = "SELECT id, remote_id FROM events WHERE source = ? AND expanded = 1 AND start_ts >= ? AND start_ts < ?"
        params: list[Any] = [source_id, start_ts, end_ts]
        if calendar is not None:
            sql += " AND calendar = ?"
            params.append(calendar)
        removed = 0
        for row in self.app.store.query(sql, params):
            if row["remote_id"] not in keep_remote_ids:
                self._remove_row(row["id"])
                removed += 1
        return removed

    # -- sync --------------------------------------------------------------------
    def sync(self, source_id: str | None = None) -> dict[str, Any]:
        summary: dict[str, Any] = {"events": 0, "errors": []}
        if not self._sync_lock.acquire(blocking=False):
            summary["errors"].append("a sync is already running")
            return summary
        try:
            for source in self.sources():
                if source_id and source["id"] != source_id:
                    continue
                try:
                    if source["kind"] == "ics":
                        if self.app.net.online:
                            summary["events"] += self._sync_ics(source)
                    elif source["kind"] == "google":
                        if self.app.net.online:
                            summary["events"] += self.google.sync(source)
                    elif source["kind"] == "phone":
                        summary["events"] += self._sync_phone(source)
                except ToolError as exc:
                    summary["errors"].append(f"{source.get('name', source['id'])}: {exc}")
                except Exception as exc:  # one bad source must not block the others
                    log.exception("calendar sync failed for %s", source["id"])
                    summary["errors"].append(f"{source.get('name', source['id'])}: {exc}")
        finally:
            self._sync_lock.release()
        self.app.bus.publish("calendar.synced", {"events": summary["events"]})
        return summary

    def _sync_ics(self, source: dict[str, Any]) -> int:
        url = (source.get("url") or "").strip()
        if not url:
            raise ToolError("this ICS calendar has no URL")
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        meta_key = f"calendar.ics.{source['id']}"
        meta = self.app.store.get_meta(meta_key, {})
        headers = {"User-Agent": "felix-assistant", "Accept": "text/calendar, */*"}
        if meta.get("etag") and meta.get("url") == url:
            headers["If-None-Match"] = meta["etag"]
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
                text = response.read().decode("utf-8", "replace")
                etag = response.headers.get("ETag", "")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return 0
            raise ToolError(f"the feed answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"could not fetch the feed ({getattr(exc, 'reason', exc)})") from exc
        if "BEGIN:VCALENDAR" not in text.upper():
            raise ToolError("that address did not return a calendar")
        events = ics.parse_ics(text, self.app.clock.tz)
        self.import_ics_events(source["id"], events)
        self.app.store.set_meta(meta_key, {"etag": etag, "url": url, "count": len(events)})
        return len(events)

    def import_ics_events(self, source_id: str, events: list[ics.IcsEvent]) -> None:
        """Replace everything cached for a feed with a freshly parsed copy."""
        with self.app.store.tx():
            for row in self.app.store.query("SELECT id FROM events WHERE source = ?", (source_id,)):
                self._remove_row(row["id"])
            for event in events:
                assert event.start is not None and event.end is not None
                self._insert(source_id, {
                    "remote_id": f"{event.uid}|{int(event.recurrence_id.timestamp()) if event.recurrence_id else 0}",
                    "uid": event.uid, "title": event.title, "description": event.description,
                    "location": event.location, "start_ts": event.start.timestamp(), "end_ts": event.end.timestamp(),
                    "all_day": int(event.all_day), "tz": getattr(event.start.tzinfo, "key", "") or "",
                    "rrule": event.rrule, "exdates": [d.timestamp() for d in event.exdates],
                    "recurrence_id": event.recurrence_id.timestamp() if event.recurrence_id else None,
                    "attendees": event.attendees, "organizer": event.organizer, "status": event.status,
                    "reminders": event.reminders, "url": event.url, "my_response": self._my_response(event.attendees),
                })

    def _my_response(self, attendees: list[dict[str, str]]) -> str:
        own = self.app.mail.own_addresses()
        for attendee in attendees:
            if attendee.get("email", "").lower() in own:
                return attendee.get("status", "")
        return ""

    # -- phone calendar (Android CalendarContract through the companion app) ---------
    def _window(self) -> tuple[datetime, datetime]:
        now = self.app.clock.now()
        return now - timedelta(days=SYNC_BACK_DAYS), now + timedelta(days=SYNC_AHEAD_DAYS)

    def _sync_phone(self, source: dict[str, Any]) -> int:
        phone = getattr(self.app, "phone", None)
        if phone is None or not phone.connected():
            return 0
        start, end = self._window()
        data = phone.request("calendar.list", {"start_ms": int(start.timestamp() * 1000),
                                               "end_ms": int(end.timestamp() * 1000)}, timeout=40)
        wanted = set(source.get("calendars") or [])
        seen: set[str] = set()
        for item in data.get("events", []):
            if wanted and str(item.get("calendar_id")) not in wanted and item.get("calendar") not in wanted:
                continue
            remote_id = f"{item['id']}:{item.get('instance_start_ms', item['start_ms'])}"
            seen.add(remote_id)
            self.upsert_remote(source["id"], remote_id, self._phone_values(item))
        self.prune(source["id"], seen, start.timestamp(), end.timestamp())
        return len(seen)

    def _phone_values(self, item: dict[str, Any]) -> dict[str, Any]:
        attendees = [{"name": a.get("name", ""), "email": (a.get("email") or "").lower(),
                      "status": (a.get("status") or "").lower()} for a in item.get("attendees") or []]
        return {
            "series_id": str(item["id"]), "calendar": item.get("calendar") or "", "title": item.get("title") or "",
            "description": item.get("description") or "", "location": item.get("location") or "",
            "start_ts": item["start_ms"] / 1000, "end_ts": item["end_ms"] / 1000,
            "all_day": int(bool(item.get("all_day"))), "tz": item.get("tz") or "", "attendees": attendees,
            "organizer": item.get("organizer") or "", "status": "confirmed",
            "my_response": self._my_response(attendees),
        }

    def _phone(self):
        phone = getattr(self.app, "phone", None)
        if phone is None or not phone.connected():
            raise ToolError("Your phone is not connected, so its calendar cannot be changed right now.")
        return phone

    def _phone_args(self, fields: dict[str, Any]) -> dict[str, Any]:
        args: dict[str, Any] = {}
        for key in ("title", "location", "description"):
            if fields.get(key) is not None:
                args[key] = fields[key]
        if fields.get("start") is not None:
            args["start_ms"] = int(fields["start"].timestamp() * 1000)
            args["tz"] = getattr(fields["start"].tzinfo, "key", "") or ""
        if fields.get("end") is not None:
            args["end_ms"] = int(fields["end"].timestamp() * 1000)
        if fields.get("all_day") is not None:
            args["all_day"] = bool(fields["all_day"])
        if fields.get("attendees"):
            args["attendees"] = [a["email"] for a in fields["attendees"] if a.get("email")]
        if fields.get("reminders"):
            args["reminders"] = fields["reminders"]
        return args

    def _phone_create(self, source: dict[str, Any], fields: dict[str, Any]) -> int:
        if fields.get("rrule"):
            raise ToolError("Repeating events on the phone calendar are not supported yet; "
                            "create it on Felix's own calendar or Google Calendar instead.")
        args = self._phone_args(fields)
        if source.get("calendar_id"):
            args["calendar_id"] = source["calendar_id"]
        data = self._phone().request("calendar.create", args, timeout=30)
        remote_id = f"{data['id']}:{args['start_ms']}"
        return self.upsert_remote(source["id"], remote_id, {
            "series_id": str(data["id"]), "title": fields["title"], "description": fields["description"],
            "location": fields["location"], "start_ts": fields["start"].timestamp(), "end_ts": fields["end"].timestamp(),
            "all_day": int(fields["all_day"]), "attendees": fields["attendees"], "status": "confirmed",
            "my_response": "accepted", "calendar": source.get("name", ""),
        })

    def _phone_update(self, source: dict[str, Any], row: dict[str, Any], changes: dict[str, Any]) -> int:
        self._phone().request("calendar.update", {"id": row["series_id"], **self._phone_args(changes)}, timeout=30)
        self.app.store.update("events", self._column_values(changes), "id = ?", (row["id"],))
        self._index(row["id"])
        return row["id"]

    def _phone_delete(self, source: dict[str, Any], row: dict[str, Any]) -> None:
        self._phone().request("calendar.delete", {"id": row["series_id"]}, timeout=30)
        for other in self.app.store.query("SELECT id FROM events WHERE source = ? AND series_id = ?",
                                          (row["source"], row["series_id"])):
            self._remove_row(other["id"])

    # -- tools -------------------------------------------------------------------
    def _time(self, value: str | None, label: str) -> datetime | None:
        if not value:
            return None
        moment = parse_datetime_loose(value, self.app.clock.now(), self.app.config.get("owner.date_order", "dmy"))
        if moment is None:
            raise ToolError(f"I could not understand the {label} {value!r}. Use '2026-11-14 15:00' or 'tomorrow 3pm'.")
        return moment

    def _attendees(self, people: list[str] | None) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for person in people or []:
            address, name = self.app.contacts.address_of(person, "email")
            if address not in [a["email"] for a in out]:
                out.append({"email": address, "name": "" if name == address else name, "status": "pending"})
        return out

    def _sends_invites(self, args: dict[str, Any]) -> bool:
        """True when carrying out this call would email people on the owner's behalf."""
        try:
            if "event" in args:
                occurrence = self.resolve(args["event"])
                kind, has_guests = occurrence["source_kind"], bool(occurrence["attendees"])
                has_guests = has_guests or bool(args.get("attendees"))
            else:
                kind, has_guests = self.source(args.get("calendar"))["kind"], bool(args.get("attendees"))
        except ToolError:
            return False
        return has_guests and kind in ("google", "phone")

    def register(self, tools: Registry) -> None:
        risk = {"risk_for": lambda args: OUTBOUND if self._sends_invites(args) else LOCAL,
                "freeze": self._freeze}
        channel = lambda args: "calendar_invite" if self._sends_invites(args) else "calendar"  # noqa: E731

        @tools.tool(
            "calendar_list",
            "Show the owner's calendar for a period, merged across all their calendars. Use for 'what's on "
            "today', 'my week', 'when is the board review', 'am I free Friday afternoon'. "
            "Give either `range` (words) or `start`/`end`. Each event has an id for calendar_update/delete.",
            {
                "range": {"type": "string", "description": "e.g. 'today', 'tomorrow', 'this week', 'next 3 days', 'friday', '14 Nov'"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "query": {"type": "string", "description": "Only events whose title, place or guests contain these words"},
            },
            risk=READ, group="calendar",
        )
        def calendar_list(args: dict[str, Any], ctx: ToolContext) -> Any:
            now = self.app.clock.now()
            label = ""
            if args.get("start"):
                start = self._time(args["start"], "start")
                end = self._time(args.get("end"), "end") or start + timedelta(days=1)
            else:
                found = parse_range(args.get("range") or "today", now, self.app.config.get("owner.date_order", "dmy"))
                if found is None:
                    raise ToolError(f"I could not understand the period {args.get('range')!r}.")
                start, end, label = found
            events = self.between(start, end, query=args.get("query"))
            timed = [e for e in events if not e["all_day"]]
            clashes = sorted({a["title"] for a in timed for b in timed
                              if a is not b and a["start_ts"] < b["end_ts"] and b["start_ts"] < a["end_ts"]})
            out: dict[str, Any] = {"period": label or f"{start:%d %b %H:%M} - {end:%d %b %H:%M}",
                                   "events": [self.public(e) for e in events], "count": len(events)}
            if clashes:
                out["overlapping"] = clashes
            return out

        @tools.tool(
            "calendar_create",
            "Put an event on the owner's calendar. Times may be ISO 8601 or plain words. Give `end` or "
            "`duration_minutes` (default 30). `attendees` are emails or contact names; on Google or phone "
            "calendars they receive an invitation, which needs the owner's approval. The result warns "
            "about clashes with existing events.",
            {
                "title": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
                "all_day": {"type": "boolean"},
                "location": {"type": "string"},
                "description": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}},
                "repeat": {"type": "string", "description": "daily, weekly, weekdays, monthly, yearly, 'every 2 weeks', or an RRULE"},
                "reminder_minutes": {"type": "integer", "minimum": 0, "maximum": 10080},
                "calendar": {"type": "string", "description": "Which calendar; default is the owner's main one"},
            },
            ["title", "start"], risk=LOCAL, channel=channel, group="calendar", extra=risk,
            recipients=lambda args, ctx: [a["email"] for a in self._attendees(args.get("attendees"))],
            preview=lambda args, ctx: self._create_preview(args),
        )
        def calendar_create(args: dict[str, Any], ctx: ToolContext) -> Any:
            start, end, all_day = self._span(args)
            try:
                rrule = repeat_to_rrule(args.get("repeat", "")) if args.get("repeat") else None
            except RuleError as exc:
                raise ToolError(str(exc)) from exc
            clashes = [] if all_day else self.conflicts(start, end)
            occurrence = self.create(
                args["title"], start, end, all_day=all_day, location=args.get("location", ""),
                description=args.get("description", ""), attendees=self._attendees(args.get("attendees")),
                rrule=rrule, reminders=[args["reminder_minutes"]] if "reminder_minutes" in args else None,
                source_id=args.get("calendar"))
            message = f"Added: {occurrence['title']}, {self.when(occurrence)}"
            if rrule:
                message += f" ({describe(rrule)})"
            if clashes:
                message += ". Note: it overlaps " + ", ".join(f"{c['title']} ({self.when(c)})" for c in clashes[:3])
            return {"message": message + ".", "event": self.public(occurrence)}

        @tools.tool(
            "calendar_update",
            "Change an existing event: rename, move, lengthen, relocate or change guests. Identify it by id "
            "or by words from its title (optionally with a day, e.g. 'board review friday'). For a repeating "
            "event only that occurrence changes unless whole_series is true.",
            {
                "event": {"type": "string", "description": "Event id, or title words"},
                "title": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
                "location": {"type": "string"},
                "description": {"type": "string"},
                "attendees": {"type": "array", "items": {"type": "string"}, "description": "Replaces the guest list"},
                "whole_series": {"type": "boolean"},
            },
            ["event"], risk=LOCAL, channel=channel, group="calendar", extra=risk,
            preview=lambda args, ctx: self._update_preview(args),
        )
        def calendar_update(args: dict[str, Any], ctx: ToolContext) -> Any:
            occurrence = self.resolve(args["event"])
            changes = self._changes(occurrence, args)
            if not changes:
                raise ToolError("Nothing to change - give a new time, title, place or guest list.")
            clashes = []
            if "start" in changes or "end" in changes:
                clashes = self.conflicts(changes.get("start", occurrence["start"]), changes.get("end", occurrence["end"]),
                                         ignore_row=occurrence["row_id"])
            updated = self.update(occurrence, changes, whole_series=bool(args.get("whole_series")))
            message = f"Updated: {updated['title']}, {self.when(updated)}"
            if clashes:
                message += ". Note: it now overlaps " + ", ".join(c["title"] for c in clashes[:3])
            return {"message": message + ".", "event": self.public(updated)}

        @tools.tool(
            "calendar_delete",
            "Remove an event from the calendar. For a repeating event only that occurrence is removed unless "
            "whole_series is true. If the event has guests on a Google or phone calendar they are notified, "
            "which needs the owner's approval.",
            {"event": {"type": "string", "description": "Event id, or title words"}, "whole_series": {"type": "boolean"}},
            ["event"], risk=LOCAL, channel=channel, group="calendar", extra=risk,
            preview=lambda args, ctx: "Cancel event\n" + self._line(self.resolve(args["event"])),
        )
        def calendar_delete(args: dict[str, Any], ctx: ToolContext) -> Any:
            occurrence = self.resolve(args["event"])
            self.delete(occurrence, whole_series=bool(args.get("whole_series")))
            scope = " (whole series)" if args.get("whole_series") and occurrence["recurring"] else ""
            return {"message": f"Removed: {occurrence['title']}, {self.when(occurrence)}{scope}."}

        @tools.tool(
            "calendar_free",
            "Find free time in the owner's working hours. Use for 'when am I free', 'find 45 minutes this "
            "week', or before proposing meeting times to someone.",
            {
                "range": {"type": "string", "description": "e.g. 'today', 'tomorrow', 'this week', 'next 3 days'"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 600},
            },
            risk=READ, group="calendar",
        )
        def calendar_free(args: dict[str, Any], ctx: ToolContext) -> Any:
            now = self.app.clock.now()
            if args.get("start"):
                start = self._time(args["start"], "start")
                end = self._time(args.get("end"), "end") or start + timedelta(days=1)
            else:
                found = parse_range(args.get("range") or "today", now, self.app.config.get("owner.date_order", "dmy"))
                if found is None:
                    raise ToolError(f"I could not understand the period {args.get('range')!r}.")
                start, end, _ = found
            start = max(start, now)
            minutes = args.get("duration_minutes", int(self.app.config.get("calendar.default_minutes", 30)))
            slots = self.free_slots(start, end, minutes)
            return {
                "duration_minutes": minutes,
                "free": [{"from": a.isoformat(timespec="minutes"), "until": b.isoformat(timespec="minutes"),
                          "text": format_when(a, b, now)} for a, b in slots],
                "note": "" if slots else "No gap of that length inside working hours in that period.",
            }

        @tools.tool(
            "calendar_sync",
            "Refresh calendars from Google, ICS feeds and the phone right now.",
            {}, risk=LOCAL, group="calendar",
        )
        def calendar_sync(args: dict[str, Any], ctx: ToolContext) -> Any:
            result = self.sync()
            if result["errors"] and not result["events"]:
                raise ToolError("; ".join(result["errors"]))
            return {"message": f"Calendars refreshed ({result['events']} events).", **result}

    # -- helpers for tools ---------------------------------------------------------
    def _span(self, args: dict[str, Any]) -> tuple[datetime, datetime, bool]:
        now = self.app.clock.now()
        order = self.app.config.get("owner.date_order", "dmy")
        text = args["start"]
        start = None
        all_day = bool(args.get("all_day"))
        loose = None
        try:
            start = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
            if start.tzinfo is None:
                start = start.replace(tzinfo=now.tzinfo)
            if len(text.strip()) <= 10:
                all_day = True
        except ValueError:
            loose = parse_when(text, now, order)
            if loose is None:
                raise ToolError(f"I could not understand the start time {text!r}.") from None
            start = loose.start
            if not loose.has_time and "all_day" not in args:
                all_day = True
        if all_day:
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = self._time(args.get("end"), "end time")
        if end is None and loose is not None and loose.end is not None:
            end = loose.end
        if end is None:
            if all_day:
                end = start + timedelta(days=1)
            else:
                end = start + timedelta(minutes=args.get("duration_minutes")
                                        or int(self.app.config.get("calendar.default_minutes", 30)))
        elif all_day:
            end = end.replace(hour=0, minute=0, second=0, microsecond=0)
            if end <= start:
                end = start + timedelta(days=1)
        if end <= start:
            raise ToolError("The end time must be after the start time.")
        return start, end, all_day

    def _changes(self, occurrence: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        for key in ("title", "location", "description"):
            if args.get(key) is not None:
                changes[key] = args[key]
        length = occurrence["end"] - occurrence["start"]
        start = self._time(args.get("start"), "start time")
        end = self._time(args.get("end"), "end time")
        if args.get("duration_minutes"):
            length = timedelta(minutes=args["duration_minutes"])
            end = (start or occurrence["start"]) + length
        if start is not None:
            changes["start"] = start
            changes["end"] = end or start + length
        elif end is not None:
            changes["end"] = end
        if changes.get("end") and changes["end"] <= changes.get("start", occurrence["start"]):
            raise ToolError("The end time must be after the start time.")
        if args.get("attendees") is not None:
            changes["attendees"] = self._attendees(args["attendees"])
        return changes

    def _line(self, occurrence: dict[str, Any]) -> str:
        guests = ", ".join(a.get("name") or a.get("email", "") for a in occurrence["attendees"][:6])
        return f"{occurrence['title']} - {self.when(occurrence)}" + (f"\nGuests: {guests}" if guests else "")

    def _create_preview(self, args: dict[str, Any]) -> str:
        start, end, all_day = self._span(args)
        guests = ", ".join(a["email"] for a in self._attendees(args.get("attendees")))
        lines = ["Create event and invite guests", f"{args['title']} - {format_when(start, None if all_day else end, self.app.clock.now(), all_day)}"]
        if args.get("location"):
            lines.append(f"Where: {args['location']}")
        if guests:
            lines.append(f"Guests: {guests}")
        if (args.get("description") or "").strip():
            lines.append("Notes guests will see: " + _shown(args["description"]))
        return "\n".join(lines)

    def _update_preview(self, args: dict[str, Any]) -> str:
        occurrence = self.resolve(args["event"])
        changes = self._changes(occurrence, args)
        lines = ["Change event (guests will be notified)", self._line(occurrence)]
        if "start" in changes or "end" in changes:
            new_start, new_end = changes.get("start", occurrence["start"]), changes.get("end", occurrence["end"])
            lines.append(f"New time: {format_when(new_start, new_end, self.app.clock.now())}")
        for key in ("title", "location"):
            if key in changes:
                lines.append(f"New {key}: {changes[key]}")
        if "attendees" in changes:
            lines.append("New guests: " + ", ".join(a["email"] for a in changes["attendees"]))
        if (args.get("description") or "").strip():
            lines.append("New notes guests will see: " + _shown(args["description"]))
        return "\n".join(lines)

    def _freeze(self, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Pin down which event and which guests a call means right now: a few words of a
        title become that event's id, guest names become addresses."""
        frozen = dict(args)
        if args.get("attendees"):
            frozen["attendees"] = [a["email"] for a in self._attendees(args["attendees"])]
        if args.get("event"):
            frozen["event"] = str(self.resolve(args["event"])["id"])
        return frozen


def _shown(text: str, limit: int = 1500) -> str:
    """Text for an approval preview: all of it, or a clear note of how much is left out."""
    text = text.strip()
    return text if len(text) <= limit else f"{text[:limit]} [... {len(text) - limit} more characters not shown]"
