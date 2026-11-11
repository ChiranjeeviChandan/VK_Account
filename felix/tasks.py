"""The owner's to-do list, including items Felix suggests from messages."""

from __future__ import annotations

import difflib
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .timeparse import format_when, parse_datetime_loose
from .tools import LOCAL, READ, Registry, ToolContext, ToolError
from .util import fold

if TYPE_CHECKING:  # pragma: no cover
    from .app import Felix

PRIORITY_NAMES = {1: "high", 2: "normal", 3: "low"}
PRIORITY_VALUES = {"high": 1, "urgent": 1, "normal": 2, "medium": 2, "low": 3}


class Tasks:
    def __init__(self, app: "Felix") -> None:
        self.app = app

    # -- core ------------------------------------------------------------------
    def add(self, title: str, due: datetime | None = None, remind: datetime | None = None,
            priority: int = 2, notes: str | None = None, origin: str = "manual",
            origin_ref: str | None = None, suggested: bool = False) -> dict[str, Any]:
        title = " ".join((title or "").split())
        if not title:
            raise ToolError("A task needs a title.")
        if origin_ref:
            dup = self.app.store.one(
                "SELECT * FROM tasks WHERE origin = ? AND origin_ref = ? AND title = ?", (origin, origin_ref, title)
            )
            if dup:
                return dup
        task_id = self.app.store.insert("tasks", {
            "title": title, "notes": notes, "due_ts": due.timestamp() if due else None,
            "remind_ts": remind.timestamp() if remind else None, "priority": priority,
            "status": "open", "origin": origin, "origin_ref": origin_ref,
            "suggested": int(suggested), "created_at": self.app.clock.ts(),
        })
        self.app.store.index("task", task_id, title, notes or "", "", self.app.clock.ts())
        self.app.bus.publish("tasks.changed", {"id": task_id})
        return self.get(task_id)  # type: ignore[return-value]

    def get(self, task_id: int) -> dict[str, Any] | None:
        return self.app.store.one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    def list(self, status: str = "open", include_suggested: bool = False, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks WHERE 1 = 1"
        params: list[Any] = []
        if status != "all":
            sql += " AND status = ?"
            params.append(status)
        if not include_suggested:
            sql += " AND suggested = 0"
        sql += " ORDER BY (due_ts IS NULL), due_ts, priority, id LIMIT ?"
        params.append(limit)
        return self.app.store.query(sql, params)

    def suggestions(self) -> list[dict[str, Any]]:
        return self.app.store.query(
            "SELECT * FROM tasks WHERE status = 'open' AND suggested = 1 ORDER BY created_at DESC LIMIT 50"
        )

    def find(self, reference: str | int) -> dict[str, Any]:
        """Resolve '#3', 3 or a few words of the title to one open task."""
        text = str(reference).strip().lstrip("#")
        if text.isdigit():
            task = self.get(int(text))
            if task:
                return task
            raise ToolError(f"There is no task #{text}.")
        folded = fold(text)
        candidates = self.list("open", include_suggested=True)
        exact = [t for t in candidates if fold(t["title"]) == folded]
        if len(exact) == 1:
            return exact[0]
        partial = [t for t in candidates if folded in fold(t["title"])]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            names = "; ".join(f"#{t['id']} {t['title']}" for t in partial[:5])
            raise ToolError(f"More than one task matches {text!r}: {names}")
        titles = {fold(t["title"]): t for t in candidates}
        close = difflib.get_close_matches(folded, list(titles), n=1, cutoff=0.6)
        if close:
            return titles[close[0]]
        raise ToolError(f"No open task matches {text!r}.")

    def complete(self, task_id: int) -> dict[str, Any]:
        self.app.store.update("tasks", {"status": "done", "done_at": self.app.clock.ts(), "suggested": 0},
                              "id = ?", (task_id,))
        self.app.bus.publish("tasks.changed", {"id": task_id})
        return self.get(task_id)  # type: ignore[return-value]

    def update(self, task_id: int, **fields: Any) -> dict[str, Any]:
        allowed = {k: v for k, v in fields.items()
                   if k in ("title", "notes", "due_ts", "remind_ts", "priority", "status", "suggested", "reminded")}
        if allowed:
            self.app.store.update("tasks", allowed, "id = ?", (task_id,))
            task = self.get(task_id)
            if task:
                self.app.store.index("task", task_id, task["title"], task["notes"] or "", "", task["created_at"] or 0)
            self.app.bus.publish("tasks.changed", {"id": task_id})
        return self.get(task_id)  # type: ignore[return-value]

    def drop(self, task_id: int) -> None:
        self.app.store.update("tasks", {"status": "dropped"}, "id = ?", (task_id,))
        self.app.store.unindex("task", task_id)
        self.app.bus.publish("tasks.changed", {"id": task_id})

    # -- views used by briefs ----------------------------------------------------
    def agenda(self, now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
        """Open tasks bucketed the way a brief presents them."""
        now = now or self.app.clock.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        buckets: dict[str, list[dict[str, Any]]] = {"overdue": [], "today": [], "upcoming": [], "someday": []}
        for task in self.list("open"):
            due = task["due_ts"]
            if due is None:
                buckets["someday"].append(task)
            elif due < start.timestamp():
                buckets["overdue"].append(task)
            elif due < end.timestamp():
                buckets["today"].append(task)
            else:
                buckets["upcoming"].append(task)
        for key in buckets:
            buckets[key].sort(key=lambda t: (t["priority"], t["due_ts"] or 0, t["id"]))
        return buckets

    def done_between(self, start_ts: float, end_ts: float) -> list[dict[str, Any]]:
        return self.app.store.query(
            "SELECT * FROM tasks WHERE status = 'done' AND done_at >= ? AND done_at < ? ORDER BY done_at",
            (start_ts, end_ts),
        )

    def due_reminders(self) -> list[dict[str, Any]]:
        return self.app.store.query(
            "SELECT * FROM tasks WHERE status = 'open' AND reminded = 0 AND remind_ts IS NOT NULL AND remind_ts <= ?",
            (self.app.clock.ts(),),
        )

    def describe(self, task: dict[str, Any]) -> str:
        now = self.app.clock.now()
        text = f"#{task['id']} {task['title']}"
        if task["due_ts"]:
            due = self.app.clock.from_ts(task["due_ts"])
            midnight = due.hour == 0 and due.minute == 0
            text += f" (due {format_when(due, None, now, all_day=midnight).replace(', all day', '')})"
        if task["priority"] == 1:
            text += " [high]"
        return text

    def public(self, task: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": task["id"], "title": task["title"], "status": task["status"],
            "priority": PRIORITY_NAMES.get(task["priority"], "normal"),
        }
        if task["due_ts"]:
            out["due"] = self.app.clock.from_ts(task["due_ts"]).isoformat(timespec="minutes")
        if task["remind_ts"]:
            out["remind_at"] = self.app.clock.from_ts(task["remind_ts"]).isoformat(timespec="minutes")
        if task["notes"]:
            out["notes"] = task["notes"]
        if task["suggested"]:
            out["suggested_by_felix"] = True
        if task["origin"] and task["origin"] != "manual":
            out["origin"] = task["origin"]
        return out

    def _time(self, value: str | None, field: str) -> datetime | None:
        if not value:
            return None
        parsed = parse_datetime_loose(value, self.app.clock.now(), self.app.config.get("owner.date_order", "dmy"))
        if parsed is None:
            raise ToolError(f"I could not understand the {field} {value!r}. Try '2026-11-14 17:00' or 'tomorrow 5pm'.")
        return parsed

    # -- tools -----------------------------------------------------------------
    def register(self, tools: Registry) -> None:
        @tools.tool(
            "task_add",
            "Add an item to the owner's to-do list. Use it when they say to remember to do something, "
            "ask for a reminder, or accept an action item. Times may be ISO 8601 or plain words ('tomorrow 5pm').",
            {
                "title": {"type": "string", "description": "Short imperative, e.g. 'Send Q3 deck to Meera'"},
                "due": {"type": "string", "description": "Deadline"},
                "remind_at": {"type": "string", "description": "When to nudge the owner"},
                "priority": {"type": "string", "enum": ["high", "normal", "low"]},
                "notes": {"type": "string"},
            },
            ["title"], risk=LOCAL, group="tasks",
        )
        def task_add(args: dict[str, Any], ctx: ToolContext) -> Any:
            due = self._time(args.get("due"), "due time")
            remind = self._time(args.get("remind_at"), "reminder time")
            task = self.add(args["title"], due=due, remind=remind,
                            priority=PRIORITY_VALUES.get(args.get("priority", "normal"), 2),
                            notes=args.get("notes"), origin="manual" if ctx.actor == "owner" else "felix")
            return {"message": f"Added to your list: {self.describe(task)}", "task": self.public(task)}

        @tools.tool(
            "task_list",
            "List the owner's to-do items. Call this for 'what's on my plate', 'my tasks', 'what is overdue'.",
            {
                "status": {"type": "string", "enum": ["open", "done", "all"], "description": "Default open"},
                "include_suggestions": {"type": "boolean", "description": "Also list action items Felix extracted but the owner has not accepted"},
            },
            risk=READ, group="tasks",
        )
        def task_list(args: dict[str, Any], ctx: ToolContext) -> Any:
            rows = self.list(args.get("status", "open"), include_suggested=bool(args.get("include_suggestions")))
            agenda = self.agenda()
            return {
                "tasks": [self.public(t) for t in rows],
                "overdue": len(agenda["overdue"]), "due_today": len(agenda["today"]),
            }

        @tools.tool(
            "task_complete",
            "Mark a to-do item done. Identify it by number or by a few words of its title.",
            {"task": {"type": "string", "description": "Task number or title words"}},
            ["task"], risk=LOCAL, group="tasks",
        )
        def task_complete(args: dict[str, Any], ctx: ToolContext) -> Any:
            task = self.find(args["task"])
            self.complete(task["id"])
            return {"message": f"Done: {task['title']}"}

        @tools.tool(
            "task_update",
            "Change a to-do item: rename it, move its deadline or reminder, change priority, or accept a suggested item.",
            {
                "task": {"type": "string", "description": "Task number or title words"},
                "title": {"type": "string"},
                "due": {"type": "string"},
                "remind_at": {"type": "string"},
                "priority": {"type": "string", "enum": ["high", "normal", "low"]},
                "notes": {"type": "string"},
                "accept": {"type": "boolean", "description": "Accept a Felix-suggested item onto the real list"},
            },
            ["task"], risk=LOCAL, group="tasks",
        )
        def task_update(args: dict[str, Any], ctx: ToolContext) -> Any:
            task = self.find(args["task"])
            fields: dict[str, Any] = {}
            if args.get("title"):
                fields["title"] = args["title"]
            if "due" in args:
                due = self._time(args.get("due"), "due time")
                fields["due_ts"] = due.timestamp() if due else None
            if "remind_at" in args:
                remind = self._time(args.get("remind_at"), "reminder time")
                fields["remind_ts"] = remind.timestamp() if remind else None
                fields["reminded"] = 0
            if args.get("priority"):
                fields["priority"] = PRIORITY_VALUES[args["priority"]]
            if "notes" in args:
                fields["notes"] = args["notes"]
            if args.get("accept"):
                fields["suggested"] = 0
            updated = self.update(task["id"], **fields)
            return {"message": f"Updated: {self.describe(updated)}", "task": self.public(updated)}

        @tools.tool(
            "task_delete",
            "Remove a to-do item that is no longer needed (not the same as completing it).",
            {"task": {"type": "string", "description": "Task number or title words"}},
            ["task"], risk=LOCAL, group="tasks",
        )
        def task_delete(args: dict[str, Any], ctx: ToolContext) -> Any:
            task = self.find(args["task"])
            self.drop(task["id"])
            return {"message": f"Removed: {task['title']}"}
