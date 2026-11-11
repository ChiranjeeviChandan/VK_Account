"""Morning and evening briefs: the to-do list plus a summary of what matters.

A brief is always built from local data with a fixed template, so it works
offline and can never fail because a language model is unreachable. When
Felix is online, Claude is asked to tighten the wording; if that fails the
template text is sent as it is.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .timeparse import format_when
from .tools import LOCAL, READ, Registry, ToolContext, ToolError
from .util import clip, json_dumps, plural

if TYPE_CHECKING:  # pragma: no cover
    from .app import Felix

log = logging.getLogger("felix.briefing")

KINDS = ("morning", "evening", "now")
TITLES = {"morning": "Morning brief", "evening": "Evening wrap-up", "now": "Brief"}

POLISH_SYSTEM = """You write the daily brief an executive reads on WhatsApp in under a minute.

You get the facts as JSON plus a plain draft. Rewrite the draft so the most consequential items come
first and every line earns its place. Rules:
- Use only facts present in the JSON. Never add meetings, people, numbers or advice that are not there.
- Keep the section order: greeting line, calendar, to-do, what's broken, waiting on you, the rest.
- WhatsApp formatting only: *bold* for section titles, the bullet "•" for items. No markdown headings, no tables.
- Names, times and titles must be copied exactly.
- Stay under the character limit you are given. Cut the least important lines first.
- If something is broken or a VIP is waiting, say so in the first three lines.
Output only the brief text."""


class Briefing:
    def __init__(self, app: "Felix") -> None:
        self.app = app
        self._retry_lock = threading.Lock()
        app.bus.on("net.changed", lambda topic, data: data.get("online") and self._retry_quietly())
        app.bus.on("phone.connected", lambda topic, data: self._retry_quietly())
        app.bus.on("whatsapp.bridge", lambda topic, data: data.get("state") == "open" and self._retry_quietly())

    # -- facts ---------------------------------------------------------------------
    def gather(self, kind: str = "now") -> dict[str, Any]:
        if kind not in KINDS:
            raise ToolError("A brief is morning, evening or now.")
        app = self.app
        now = app.clock.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        focus_start = today + timedelta(days=1) if kind == "evening" else today
        focus_from = now if kind == "now" else focus_start
        agenda = app.tasks.agenda(now)
        tomorrow_start = (today + timedelta(days=1)).timestamp()
        tomorrow_end = (today + timedelta(days=2)).timestamp()
        pulse = app.pulse.report()
        chats = app.whatsapp.chats(unread_only=True, limit=50)
        calls = app.phone.missed_calls(24)
        pending_outbox = app.outbox.pending()
        failed_outbox = app.store.scalar(
            "SELECT COUNT(*) FROM outbox WHERE status = 'failed' AND created_at >= ?", (now.timestamp() - 86400,), 0)
        hour = now.hour
        greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
        return {
            "kind": kind, "generated_at": now.isoformat(timespec="minutes"), "date": now.strftime("%a %d %b"),
            "owner": app.owner_name(), "greeting": greeting, "online": app.net.online,
            "day": self._day("Tomorrow" if kind == "evening" else "Today", focus_from, focus_start + timedelta(days=1)),
            "tasks": {
                "overdue": [self._task(t) for t in agenda["overdue"]],
                "today": [self._task(t) for t in agenda["today"]],
                "tomorrow": [self._task(t) for t in agenda["upcoming"] if tomorrow_start <= t["due_ts"] < tomorrow_end],
                "important": [self._task(t) for t in agenda["someday"] if t["priority"] == 1][:5],
                "done_today": [self._task(t) for t in app.tasks.done_between(today.timestamp(), now.timestamp() + 1)],
                "open": sum(len(v) for v in agenda.values()),
                "suggested": len(app.tasks.suggestions()),
            },
            "inbox": {"unread": app.mail.unread_count(), "accounts": len(app.mail.accounts())},
            "chats": {"unread": sum(c["unread"] or 0 for c in chats), "chats": len(chats),
                      "from": [c["name"] for c in chats[:5]]},
            "missed_calls": [c["title"].replace("Missed call from ", "") for c in calls][:5],
            "broken": pulse["broken"][:6], "recovered": pulse["recovered"][:3], "wins": pulse["wins"][:3],
            "waiting_on_you": pulse["waiting_on_you"][:8], "mood": pulse["mood"], "news": pulse["news"][:3],
            "tone_worsening": [p["who"] for p in pulse["people"] if p.get("tone") == "worsening"][:3],
            "approvals_pending": len(app.policy.pending()),
            "outbox": {"queued": len(pending_outbox), "failed": int(failed_outbox)},
        }

    def tomorrow(self) -> dict[str, Any]:
        """Tomorrow's meetings, for the dashboard once today's are over."""
        start = self.app.clock.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return self._day("Tomorrow", start, start + timedelta(days=1))

    def _day(self, label: str, start: datetime, end: datetime) -> dict[str, Any]:
        events = self.app.calendar.between(start, end)
        timed = [e for e in events if not e["all_day"]]
        overlaps = sorted({f"{a['title']} / {b['title']}" for i, a in enumerate(timed) for b in timed[i + 1:]
                           if a["start_ts"] < b["end_ts"] and b["start_ts"] < a["end_ts"]})
        return {"label": label, "events": [self._event(e) for e in events], "count": len(timed),
                "first": self._event(timed[0]) if timed else None, "overlaps": overlaps}

    def _event(self, event: dict[str, Any]) -> dict[str, Any]:
        now = self.app.clock.now()
        text = format_when(event["start"], None if event["all_day"] else event["end"], now, all_day=event["all_day"])
        for prefix in ("today ", "tomorrow "):
            if text.startswith(prefix):
                text = text[len(prefix):]
        out = {"title": event["title"], "time": "All day" if event["all_day"] else text,
               "all_day": event["all_day"]}
        if event["location"]:
            out["location"] = event["location"]
        if event["attendees"]:
            out["with"] = [a.get("name") or a.get("email") for a in event["attendees"]][:4]
        return out

    def _task(self, task: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"id": task["id"], "title": task["title"]}
        if task["due_ts"]:
            due = self.app.clock.from_ts(task["due_ts"])
            out["due"] = due.isoformat(timespec="minutes")
            out["day"] = due.strftime("%a %d %b").replace(" 0", " ")
            if not (due.hour == 0 and due.minute == 0):
                out["time"] = due.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
        if task["priority"] == 1:
            out["high"] = True
        return out

    # -- text ------------------------------------------------------------------------
    def render(self, data: dict[str, Any], style: str = "whatsapp") -> str:
        """The template brief. ``style`` is 'whatsapp' (*bold* titles) or 'plain'."""
        def title(text: str) -> str:
            return f"*{text}*" if style == "whatsapp" else text.upper()

        kind = data["kind"]
        name = f", {data['owner']}" if data["owner"] else ""
        lines: list[str] = []
        head = "Evening wrap-up" if kind == "evening" else data["greeting"]
        lines.append(f"{title(head + name)} - {data['date']}")

        def calendar_block() -> None:
            day = data["day"]
            label = day["label"] if kind != "now" else "Rest of today"
            if not day["events"]:
                lines.extend(["", f"{title(label)} - no meetings."])
                return
            summary = f"{day['count']} meeting{'s' if day['count'] != 1 else ''}"
            if day["first"]:
                summary += f", first at {day['first']['time'].split('–')[0]}"
            lines.extend(["", f"{title(label)} - {summary}"])
            for event in day["events"][:8]:
                where = f" ({event['location']})" if event.get("location") else ""
                lines.append(f"• {event['time']}: {event['title']}{where}" if event["all_day"]
                             else f"• {event['time']}  {event['title']}{where}")
            if len(day["events"]) > 8:
                lines.append(f"• ...and {len(day['events']) - 8} more")
            for clash in day["overlaps"][:2]:
                lines.append(f"Clash: {clash} overlap.")

        def task_line(task: dict[str, Any], prefix: str = "") -> str:
            if prefix and task.get("day"):          # overdue: the day it was due says more than the hour
                when = f" (was due {task['day']})"
            else:
                when = f" ({task['time']})" if task.get("time") else ""
            flag = " [high]" if task.get("high") else ""
            return f"• {prefix}{task['title']}{when}{flag}"

        tasks = data["tasks"]
        if kind == "evening":
            if tasks["done_today"]:
                lines.extend(["", f"{title('Done today')} ({len(tasks['done_today'])})"])
                lines.extend(task_line(t) for t in tasks["done_today"][:6])
            still = tasks["overdue"] + tasks["today"]
            if still:
                lines.extend(["", f"{title('Still open')} ({len(still)})"])
                lines.extend(task_line(t, "Overdue: " if t in tasks["overdue"] else "") for t in still[:6])
        else:
            calendar_block()
            due = tasks["overdue"] + tasks["today"]
            if due:
                parts = []
                if tasks["overdue"]:
                    parts.append(f"{len(tasks['overdue'])} overdue")
                if tasks["today"]:
                    parts.append(f"{len(tasks['today'])} due today")
                lines.extend(["", f"{title('To do')} - {', '.join(parts)}"])
                lines.extend(task_line(t, "Overdue: " if t in tasks["overdue"] else "") for t in due[:7])
                if len(due) > 7:
                    lines.append(f"• ...and {len(due) - 7} more")
            elif tasks["important"]:
                lines.extend(["", f"{title('To do')} - nothing due today; high priority:"])
                lines.extend(task_line(t) for t in tasks["important"][:3])
            else:
                lines.extend(["", f"{title('To do')} - nothing due today."])

        shown: set[tuple[str, int]] = set()
        if data["broken"]:
            lines.extend(["", f"{title('What is broken')} ({len(data['broken'])})"])
            for issue in data["broken"][:5]:
                lines.append("• " + self.app.pulse.render_issue(issue))
                shown.update((item["kind"], item["ref_id"]) for item in issue["items"])
        else:
            lines.extend(["", "Nothing looks broken."])
        for issue in data["recovered"][:2]:
            lines.append(f"• Resolved: {clip(issue['title'], 70)}")

        waiting = [w for w in data["waiting_on_you"] if (w["kind"], w["ref_id"]) not in shown]
        if waiting:
            lines.extend(["", f"{title('Waiting on you')} ({len(waiting)})"])
            for item in waiting[:5]:
                flag = " [VIP]" if item["vip"] else ""
                urgent = " - urgent" if item["urgent"] else ""
                lines.append(f"• {item['who']}{flag}: {clip(item['title'], 70)}{urgent}")

        counts: list[str] = []
        if data["inbox"]["accounts"]:
            counts.append(f"Inbox: {data['inbox']['unread']} unread.")
        if data["chats"]["unread"]:
            chats = data["chats"]
            counts.append(f"WhatsApp: {chats['unread']} unread in {chats['chats']} chat{'s' if chats['chats'] != 1 else ''}.")
        if data["missed_calls"]:
            who = ", ".join(dict.fromkeys(data["missed_calls"]))
            counts.append(f"Missed call{'s' if len(data['missed_calls']) != 1 else ''}: {who}.")
        if counts:
            lines.extend(["", " ".join(counts)])

        if data["wins"]:
            lines.extend(["", title("Good news")])
            lines.extend(f"• {clip(w['title'], 70)}" + (f" - {w['who']}" if w["who"] else "") for w in data["wins"][:2])
        if data["tone_worsening"]:
            lines.append("")
            lines.append("Tone getting worse: " + ", ".join(data["tone_worsening"]) + ".")
        negative_news = [n for n in data["news"] if n["tone"] == "negative"]
        if negative_news:
            lines.extend(["", title("In the news")])
            lines.extend(f"• {clip(n['title'], 90)}" for n in negative_news[:2])

        if kind == "evening":
            calendar_block()
            if tasks["tomorrow"]:
                lines.extend(["", title("Due tomorrow")])
                lines.extend(task_line(t) for t in tasks["tomorrow"][:5])

        footer: list[str] = []
        if data["approvals_pending"]:
            count = data["approvals_pending"]
            footer.append(f"{count} action{'s are' if count != 1 else ' is'} waiting for your approval.")
        if data["outbox"]["queued"]:
            count = data["outbox"]["queued"]
            footer.append(f"{count} message{'s are' if count != 1 else ' is'} queued to send.")
        if data["outbox"]["failed"]:
            footer.append(f"{plural(data['outbox']['failed'], 'message')} could not be sent - check the dashboard.")
        if tasks["suggested"]:
            footer.append(f"{plural(tasks['suggested'], 'suggested to-do')} from your messages.")
        if footer:
            lines.extend(["", " ".join(footer)])
        return self._fit("\n".join(lines))

    def _fit(self, text: str) -> str:
        limit = int(self.app.config.get("briefs.max_chars", 3500))
        if len(text) <= limit:
            return text
        kept: list[str] = []
        used = 0
        for line in text.split("\n"):
            if used + len(line) + 1 > limit - 40:
                break
            kept.append(line)
            used += len(line) + 1
        return "\n".join(kept).rstrip() + "\n...more in the Felix dashboard."

    def compose(self, kind: str = "now", polish: bool | None = None) -> dict[str, Any]:
        data = self.gather(kind)
        text = self.render(data)
        engine = "template"
        want_polish = self.app.config.get("briefs.polish", True) if polish is None else polish
        brain = getattr(self.app, "brain", None)
        if want_polish and brain is not None and self.app.net.online and brain.claude.available():
            limit = int(self.app.config.get("briefs.max_chars", 3500))
            try:
                polished = brain.claude.complete(
                    POLISH_SYSTEM,
                    f"Character limit: {limit}\n\nFacts (JSON):\n{json_dumps(data)}\n\nDraft:\n{text}",
                    max_tokens=4000)
                if polished and len(polished) <= limit + 200 and len(polished) > 40:
                    text, engine = polished.strip(), "claude"
            except Exception as exc:      # the template text is always good enough to send
                log.warning("brief polish skipped: %s", exc)
        return {"kind": kind, "day": self.app.clock.now().strftime("%Y-%m-%d"), "text": text, "data": data,
                "engine": engine}

    # -- delivery --------------------------------------------------------------------
    def deliver(self, text: str, kind: str) -> dict[str, Any]:
        """Send through the first configured channel that works."""
        app = self.app
        errors: list[str] = []
        heading = f"Felix - {TITLES.get(kind, 'Brief').lower()}"
        for channel in app.config.get("briefs.channels", ["whatsapp", "phone", "email", "desktop"]):
            try:
                if channel == "whatsapp":
                    return {"via": app.whatsapp.send_to_owner(text), "errors": errors}
                if channel == "phone":
                    if not app.phone.connected():
                        raise ToolError("phone not connected")
                    first = next((line for line in text.split("\n")[1:] if line.strip()), "")
                    app.phone.request("notify", {"title": heading, "text": clip(first.replace("*", ""), 120),
                                                 "big_text": text.replace("*", ""), "tag": f"brief-{kind}"}, timeout=20)
                    return {"via": "phone notification", "errors": errors}
                if channel == "email":
                    address = app.config.get("owner.email") or (app.mail.accounts()[0].email if app.mail.accounts() else "")
                    if not address or not app.mail.accounts():
                        raise ToolError("no mail account")
                    if not app.net.online:
                        raise ToolError("offline")
                    result = app.mail.queue_send([address], f"{heading.title()} - {app.clock.now():%a %d %b}",
                                                 text.replace("*", ""))
                    if result["status"] != "sent":
                        raise ToolError("mail server unreachable")
                    return {"via": "email", "errors": errors}
                if channel == "desktop":
                    if app.laptop.notify(heading.title(), clip(text.replace("*", "").replace("\n", "  "), 240)):
                        return {"via": "desktop notification", "errors": errors}
                    raise ToolError("no notification service")
            except ToolError as exc:
                errors.append(f"{channel}: {exc}")
            except Exception as exc:
                log.exception("brief delivery via %s failed", channel)
                errors.append(f"{channel}: {exc}")
        return {"via": None, "errors": errors}

    def run(self, kind: str, deliver: bool = True) -> dict[str, Any]:
        """Compose, store and (optionally) deliver. Used by the scheduler and 'send my brief'."""
        brief = self.compose(kind)
        now = self.app.clock.ts()
        brief_id = self.app.store.insert("briefs", {
            "kind": kind, "day": brief["day"], "text": brief["text"], "data": brief["data"], "created_at": now,
            "status": "stored", "engine": brief["engine"]})
        outcome: dict[str, Any] = {"via": None, "errors": []}
        if deliver:
            outcome = self.deliver(brief["text"], kind)
            self.app.store.update("briefs", {
                "status": "delivered" if outcome["via"] else "pending", "delivered_via": outcome["via"],
                "delivered_at": now if outcome["via"] else None}, "id = ?", (brief_id,))
        self.app.store.audit("scheduler", f"brief.{kind}", {"via": outcome["via"], "errors": outcome["errors"][:4]},
                             "ok" if outcome["via"] or not deliver else "pending")
        self.app.bus.publish("brief.ready", {"id": brief_id, "kind": kind, "via": outcome["via"]})
        return {**brief, "id": brief_id, "via": outcome["via"], "errors": outcome["errors"]}

    def retry_pending(self) -> int:
        """Deliver briefs that had no route when they were due (laptop offline, phone away)."""
        window = float(self.app.config.get("briefs.catch_up_hours", 4)) * 3600
        now = self.app.clock.ts()
        delivered = 0
        for row in self.app.store.query(
                "SELECT * FROM briefs WHERE status = 'pending' AND created_at >= ? ORDER BY id", (now - window,)):
            outcome = self.deliver(row["text"], row["kind"])
            if outcome["via"]:
                self.app.store.update("briefs", {"status": "delivered", "delivered_via": outcome["via"],
                                                 "delivered_at": now}, "id = ?", (row["id"],))
                delivered += 1
        self.app.store.update("briefs", {"status": "missed"}, "status = 'pending' AND created_at < ?", (now - window,))
        return delivered

    def _retry_quietly(self) -> None:
        """Retry on a thread of its own. The event that triggers this is often
        published from the phone's own request thread; delivering there would
        wait on the very phone that is waiting on us."""
        if not self.app.store.scalar("SELECT 1 FROM briefs WHERE status = 'pending' LIMIT 1"):
            return

        def work() -> None:
            if not self._retry_lock.acquire(blocking=False):
                return
            try:
                self.retry_pending()
            except Exception:
                log.exception("brief retry failed")
            finally:
                self._retry_lock.release()

        threading.Thread(target=work, name="felix-brief-retry", daemon=True).start()

    def latest(self, kind: str | None = None) -> dict[str, Any] | None:
        if kind:
            return self.app.store.one("SELECT * FROM briefs WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,))
        return self.app.store.one("SELECT * FROM briefs ORDER BY id DESC LIMIT 1")

    def sent_today(self, kind: str, day: datetime) -> bool:
        return bool(self.app.store.scalar("SELECT 1 FROM briefs WHERE kind = ? AND day = ?",
                                          (kind, day.strftime("%Y-%m-%d"))))

    # -- tools -----------------------------------------------------------------------
    def register(self, tools: Registry) -> None:
        @tools.tool(
            "brief_get",
            "Gather the owner's brief from live data: calendar, to-dos, what is broken, who is waiting, unread "
            "counts, missed calls, good news. kind is 'morning' (today), 'evening' (today's wrap-up plus "
            "tomorrow) or 'now' (the rest of today). Use for 'brief me', 'how does my day look', 'summary'.",
            {"kind": {"type": "string", "enum": list(KINDS)}},
            risk=READ, group="brief",
        )
        def brief_get(args: dict[str, Any], ctx: ToolContext) -> Any:
            data = self.gather(args.get("kind", "now"))
            return {"brief_text": self.render(data), "facts": data}

        @tools.tool(
            "brief_send",
            "Compose the brief and send it to the owner's own WhatsApp now (falling back to a phone "
            "notification, email or desktop notification). Use when they ask to be sent their brief.",
            {"kind": {"type": "string", "enum": list(KINDS)}},
            risk=LOCAL, group="brief",
        )
        def brief_send(args: dict[str, Any], ctx: ToolContext) -> Any:
            result = self.run(args.get("kind", "now"))
            if result["via"]:
                return {"message": f"Brief sent via {result['via']}.", "text": result["text"]}
            return {"message": "The brief is ready but could not be delivered yet (" + "; ".join(result["errors"][:3])
                               + "). It will go out when a route is available.", "text": result["text"]}
