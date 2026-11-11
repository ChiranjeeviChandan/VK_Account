"""The clockwork: briefs at their set times, sync, reminders, retries.

One background thread ticks every few seconds; each job decides whether it is
due. A laptop that was asleep at 07:30 still sends the morning brief when it
wakes, as long as that is within the catch-up window.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from .timeparse import format_when
from .util import clip, parse_hhmm

if TYPE_CHECKING:  # pragma: no cover
    from .app import Felix

log = logging.getLogger("felix.scheduler")


class Scheduler:
    def __init__(self, app: "Felix") -> None:
        self.app = app
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._busy = threading.Lock()
        self._running: set[str] = set()

    # -- loop ----------------------------------------------------------------------
    def start(self, interval: float = 15.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:
                    log.exception("scheduler tick failed")

        self._thread = threading.Thread(target=loop, name="felix-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> list[str]:
        """Run whatever is due. Returns the names of the jobs that ran."""
        if not self._busy.acquire(blocking=False):
            return []
        ran: list[str] = []
        try:
            now = self.app.clock.now()
            for kind in ("morning", "evening"):
                if self._brief_due(kind, now):
                    self._job(f"brief.{kind}", lambda kind=kind: self.app.briefing.run(kind), ran)
            self._every("outbox.flush", 20, self.app.outbox.flush, ran, record=False)
            self._every("briefs.retry", 60, self.app.briefing.retry_pending, ran, record=False)
            self._every("reminders", 30, self._reminders, ran, record=False)
            self._every("meetings", 60, self._meeting_alerts, ran, record=False)
            self._every("approvals.expire", 300, self.app.policy.expire, ran, record=False)
            if self.app.net.online:
                if self.app.mail.accounts():
                    minutes = float(self.app.config.get("mail.sync_minutes", 3))
                    self._every("mail.sync", minutes * 60, self._mail_sync, ran, background=True)
                minutes = float(self.app.config.get("calendar.sync_minutes", 10))
                if any(s["kind"] != "local" for s in self.app.calendar.sources()):
                    self._every("calendar.sync", minutes * 60, self._calendar_sync, ran, background=True)
                if self.app.config.get("pulse.feeds"):
                    minutes = float(self.app.config.get("pulse.feed_minutes", 30))
                    self._every("feeds.sync", minutes * 60, self.app.feeds.sync, ran, background=True)
        finally:
            self._busy.release()
        return ran

    # -- job plumbing ----------------------------------------------------------------
    def _every(self, name: str, seconds: float, fn: Callable[[], Any], ran: list[str], record: bool = True,
               background: bool = False) -> None:
        last = self.app.store.scalar("SELECT last_run FROM jobs WHERE name = ?", (name,), 0.0)
        if self.app.clock.ts() - last < seconds:
            return
        if background:
            # Network jobs must not hold up briefs and reminders.
            if name in self._running:
                return
            self._running.add(name)
            self._mark(name)

            def work() -> None:
                try:
                    self._run(name, fn, record)
                finally:
                    self._running.discard(name)

            threading.Thread(target=work, name=f"felix-{name}", daemon=True).start()
            ran.append(name)
            return
        self._job(name, fn, ran, record)

    def _job(self, name: str, fn: Callable[[], Any], ran: list[str], record: bool = True) -> None:
        self._mark(name)
        self._run(name, fn, record)
        ran.append(name)

    def _mark(self, name: str) -> None:
        self.app.store.execute(
            "INSERT INTO jobs (name, last_run, runs) VALUES (?, ?, 1) "
            "ON CONFLICT(name) DO UPDATE SET last_run = excluded.last_run, runs = runs + 1",
            (name, self.app.clock.ts()))

    def _run(self, name: str, fn: Callable[[], Any], record: bool) -> None:
        try:
            result = fn()
        except Exception as exc:
            log.exception("job %s failed", name)
            self.app.store.update("jobs", {"last_error": clip(str(exc), 300)}, "name = ?", (name,))
            return
        errors = result.get("errors") if isinstance(result, dict) else None
        self.app.store.update("jobs", {"last_ok": self.app.clock.ts(),
                                       "last_error": clip("; ".join(errors), 300) if errors else None},
                              "name = ?", (name,))

    def jobs(self) -> list[dict[str, Any]]:
        return self.app.store.query("SELECT * FROM jobs ORDER BY name")

    # -- briefs ------------------------------------------------------------------------
    def brief_time(self, kind: str, day: datetime) -> datetime | None:
        config = self.app.config.get(f"briefs.{kind}", {}) or {}
        if not config.get("enabled", True):
            return None
        hour, minute = parse_hhmm(config.get("time", ""), (7, 30) if kind == "morning" else (21, 0))
        return day.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _brief_due(self, kind: str, now: datetime) -> bool:
        scheduled = self.brief_time(kind, now)
        if scheduled is None or now < scheduled:
            return False
        if not self.app.config.get("briefs.weekends", True) and now.weekday() >= 5:
            return False
        catch_up = timedelta(hours=float(self.app.config.get("briefs.catch_up_hours", 4)))
        if now - scheduled > catch_up:
            return False      # too late to be useful; the next one is not far off
        return not self.app.briefing.sent_today(kind, now)

    def next_briefs(self) -> dict[str, str]:
        """When each brief will next go out (for the status screen)."""
        now = self.app.clock.now()
        out: dict[str, str] = {}
        for kind in ("morning", "evening"):
            for offset in range(0, 8):
                day = now + timedelta(days=offset)
                scheduled = self.brief_time(kind, day)
                if scheduled is None:
                    break
                if not self.app.config.get("briefs.weekends", True) and day.weekday() >= 5:
                    continue
                if scheduled > now or (offset == 0 and self._brief_due(kind, now)):
                    out[kind] = scheduled.isoformat(timespec="minutes")
                    break
        return out

    # -- small jobs --------------------------------------------------------------------
    def _mail_sync(self) -> dict[str, Any]:
        return self.app.mail.sync()

    def _calendar_sync(self) -> dict[str, Any]:
        return self.app.calendar.sync()

    def _nudge(self, title: str, text: str) -> None:
        """Tell the owner something now, on whatever is reachable."""
        self.app.laptop.notify(title, text)
        if self.app.phone.connected():
            try:
                self.app.phone.send("notify", {"title": title, "text": text}, expires_in=300)
            except Exception:
                pass
        self.app.bus.publish("nudge", {"title": title, "text": text})

    def _reminders(self) -> int:
        due = self.app.tasks.due_reminders()
        for task in due:
            self._nudge("Reminder", task["title"])
            self.app.tasks.update(task["id"], reminded=1)
        return len(due)

    def _meeting_alerts(self) -> int:
        """Heads-up a few minutes before each meeting."""
        lead = float(self.app.config.get("calendar.remind_minutes", 10))
        if lead <= 0:
            return 0
        now = self.app.clock.now()
        sent = 0
        told: list[str] = self.app.store.get_meta("scheduler.meetings_told", [])
        for event in self.app.calendar.between(now, now + timedelta(minutes=lead)):
            key = f"{event['id']}@{int(event['start_ts'])}"
            if event["all_day"] or event["start_ts"] < now.timestamp() or key in told:
                continue
            minutes = max(1, round((event["start_ts"] - now.timestamp()) / 60))
            where = f" - {event['location']}" if event["location"] else ""
            self._nudge(f"{event['title']} in {minutes} min",
                        format_when(event["start"], event["end"], now) + where)
            told.append(key)
            sent += 1
        if sent:
            self.app.store.set_meta("scheduler.meetings_told", told[-200:])
        return sent
