"""Google Calendar through its REST API (v3), using the owner's own OAuth client."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..tools import ToolError
from .oauth import NeedsSignIn, OAuthError

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix
    from .calendar import Calendar

log = logging.getLogger("felix.calendar.google")


class GoogleCalendar:
    def __init__(self, app: "Felix", calendar: "Calendar") -> None:
        self.app = app
        self.calendar = calendar
        self.base = "https://www.googleapis.com/calendar/v3"   # tests point this at a local fake

    # -- HTTP --------------------------------------------------------------------
    def _call(self, source: dict[str, Any], method: str, path: str, query: dict[str, Any] | None = None,
              body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.app.net.online:
            raise ToolError("Google Calendar needs a connection and Felix is offline.")
        account = source.get("account") or self.app.config.get("owner.email", "")
        try:
            token = self.app.oauth.access_token("google", account)
        except (NeedsSignIn, OAuthError) as exc:
            raise ToolError(str(exc)) from exc
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            try:
                message = json.loads(detail)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = detail[:200]
            if exc.code == 410 and method == "DELETE":
                return {}     # already gone
            raise ToolError(f"Google Calendar refused ({exc.code}): {message}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"Could not reach Google Calendar ({getattr(exc, 'reason', exc)})") from exc

    # -- reading -------------------------------------------------------------------
    def calendars(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        token = None
        while True:
            page = self._call(source, "GET", "/users/me/calendarList", {"pageToken": token, "maxResults": 250})
            items += page.get("items", [])
            token = page.get("nextPageToken")
            if not token:
                break
        return [{"id": c["id"], "name": c.get("summaryOverride") or c.get("summary", c["id"]),
                 "primary": bool(c.get("primary")), "selected": bool(c.get("selected", True)),
                 "writable": c.get("accessRole") in ("owner", "writer")} for c in items]

    def _chosen(self, source: dict[str, Any]) -> list[dict[str, Any]]:
        wanted = source.get("calendars")
        available = self.calendars(source)
        if wanted:
            return [c for c in available if c["id"] in wanted or c["name"] in wanted]
        return [c for c in available if c["selected"]] or [c for c in available if c["primary"]]

    def sync(self, source: dict[str, Any]) -> int:
        start, end = self.calendar._window()
        total = 0
        for cal in self._chosen(source):
            seen: set[str] = set()
            token = None
            while True:
                page = self._call(source, "GET", f"/calendars/{urllib.parse.quote(cal['id'], safe='')}/events", {
                    "timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                    "orderBy": "startTime", "maxResults": 250, "pageToken": token})
                for item in page.get("items", []):
                    values = self._values(item, cal)
                    if values is None:
                        continue
                    remote_id = f"{cal['id']}/{item['id']}"
                    seen.add(remote_id)
                    self.calendar.upsert_remote(source["id"], remote_id, values)
                token = page.get("nextPageToken")
                if not token:
                    break
            self.calendar.prune(source["id"], seen, start.timestamp(), end.timestamp(), calendar=cal["name"])
            total += len(seen)
        return total

    def _values(self, item: dict[str, Any], cal: dict[str, Any]) -> dict[str, Any] | None:
        if item.get("status") == "cancelled" or "start" not in item:
            return None
        start, all_day = self._moment(item["start"])
        end, _ = self._moment(item.get("end") or item["start"])
        attendees = [{"name": a.get("displayName", ""), "email": (a.get("email") or "").lower(),
                      "status": _status(a.get("responseStatus"))} for a in item.get("attendees") or []]
        mine = next((a for a in item.get("attendees") or [] if a.get("self")), None)
        response = _status(mine.get("responseStatus")) if mine else ("accepted" if (item.get("organizer") or {}).get("self") else "")
        organizer = item.get("organizer") or {}
        return {
            "series_id": item.get("recurringEventId") or "", "calendar": cal["name"], "uid": item.get("iCalUID", ""),
            "title": item.get("summary", ""), "description": item.get("description", ""),
            "location": item.get("location", ""), "start_ts": start.timestamp(), "end_ts": end.timestamp(),
            "all_day": int(all_day), "tz": (item["start"].get("timeZone") or ""), "attendees": attendees,
            "organizer": organizer.get("email") or organizer.get("displayName", ""), "status": "confirmed",
            "my_response": response, "url": item.get("hangoutLink") or item.get("htmlLink", ""),
            "etag": item.get("etag", ""),
        }

    def _moment(self, value: dict[str, Any]) -> tuple[datetime, bool]:
        if "date" in value:
            day = datetime.strptime(value["date"], "%Y-%m-%d")
            return day.replace(tzinfo=self.app.clock.tz), True
        text = value["dateTime"]
        moment = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self.calendar.tz(value.get("timeZone")))
        return moment, False

    # -- writing -------------------------------------------------------------------
    def _body(self, fields: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if fields.get("title") is not None:
            body["summary"] = fields["title"]
        if fields.get("description") is not None:
            body["description"] = fields["description"]
        if fields.get("location") is not None:
            body["location"] = fields["location"]
        all_day = fields.get("all_day")
        for key in ("start", "end"):
            moment = fields.get(key)
            if moment is None:
                continue
            if all_day:
                body[key] = {"date": moment.strftime("%Y-%m-%d")}
            else:
                zone = getattr(moment.tzinfo, "key", None)
                body[key] = {"dateTime": moment.isoformat(timespec="seconds"), **({"timeZone": zone} if zone else {})}
        if fields.get("attendees") is not None:
            body["attendees"] = [{"email": a["email"], **({"displayName": a["name"]} if a.get("name") else {})}
                                 for a in fields["attendees"] if a.get("email")]
        if fields.get("rrule"):
            body["recurrence"] = ["RRULE:" + fields["rrule"]]
        if fields.get("reminders"):
            body["reminders"] = {"useDefault": False,
                                 "overrides": [{"method": "popup", "minutes": m} for m in fields["reminders"]]}
        return body

    def _target(self, source: dict[str, Any]) -> dict[str, Any]:
        wanted = source.get("write_calendar")
        calendars = self.calendars(source)
        for cal in calendars:
            if wanted and wanted in (cal["id"], cal["name"]):
                return cal
        for cal in calendars:
            if cal["primary"]:
                return cal
        writable = [c for c in calendars if c["writable"]]
        if not writable:
            raise ToolError("This Google account has no calendar Felix may write to.")
        return writable[0]

    def create(self, source: dict[str, Any], fields: dict[str, Any]) -> int:
        cal = self._target(source)
        notify = "all" if fields.get("attendees") else "none"
        item = self._call(source, "POST", f"/calendars/{urllib.parse.quote(cal['id'], safe='')}/events",
                          {"sendUpdates": notify}, self._body(fields))
        values = self._values(item, cal)
        if values is None:
            raise ToolError("Google accepted the event but returned nothing usable.")
        if fields.get("rrule"):
            # Google returns the series; its instances arrive with the next sync.
            values["series_id"] = item["id"]
        row_id = self.calendar.upsert_remote(source["id"], f"{cal['id']}/{item['id']}", values)
        if fields.get("rrule"):
            try:
                self.sync(source)
                first = self.app.store.one(
                    "SELECT id FROM events WHERE source = ? AND series_id = ? AND remote_id != ? ORDER BY start_ts LIMIT 1",
                    (source["id"], item["id"], f"{cal['id']}/{item['id']}"))
                if first:
                    self.calendar._remove_row(row_id)   # replaced by real instances
                    row_id = first["id"]
            except ToolError as exc:
                log.warning("post-create sync failed: %s", exc)
        return row_id

    def _ids(self, row: dict[str, Any], whole_series: bool) -> tuple[str, str]:
        cal_id, _, event_id = (row["remote_id"] or "").rpartition("/")
        if whole_series and row["series_id"]:
            event_id = row["series_id"]
        return cal_id, event_id

    def update(self, source: dict[str, Any], row: dict[str, Any], changes: dict[str, Any], whole_series: bool) -> int:
        cal_id, event_id = self._ids(row, whole_series)
        fields = dict(changes)
        fields.setdefault("all_day", bool(row["all_day"]))
        has_guests = bool(row["attendees"]) or bool(changes.get("attendees"))
        item = self._call(source, "PATCH",
                          f"/calendars/{urllib.parse.quote(cal_id, safe='')}/events/{urllib.parse.quote(event_id, safe='')}",
                          {"sendUpdates": "all" if has_guests else "none"}, self._body(fields))
        if whole_series and row["series_id"]:
            self.sync(source)
            return row["id"]
        values = self._values(item, {"id": cal_id, "name": row["calendar"]})
        if values is not None:
            self.calendar.upsert_remote(source["id"], row["remote_id"], values)
        return row["id"]

    def delete(self, source: dict[str, Any], row: dict[str, Any], whole_series: bool) -> None:
        cal_id, event_id = self._ids(row, whole_series)
        self._call(source, "DELETE",
                   f"/calendars/{urllib.parse.quote(cal_id, safe='')}/events/{urllib.parse.quote(event_id, safe='')}",
                   {"sendUpdates": "all" if row["attendees"] else "none"})
        if whole_series and row["series_id"]:
            for other in self.app.store.query("SELECT id FROM events WHERE source = ? AND series_id = ?",
                                              (row["source"], row["series_id"])):
                self.calendar._remove_row(other["id"])
        else:
            self.calendar._remove_row(row["id"])


def _status(value: str | None) -> str:
    return {"needsAction": "pending", "accepted": "accepted", "declined": "declined",
            "tentative": "tentative"}.get(value or "", "")

