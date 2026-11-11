"""Outbound queue: nothing the owner approved is lost while offline.

Emails, WhatsApp messages and texts are written here first, then delivered.
Offline they simply wait; when the connection returns they go out in order,
with backoff on failures.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from .tools import ToolError
from .util import clip

if TYPE_CHECKING:  # pragma: no cover
    from .app import Felix

log = logging.getLogger("felix.outbox")

BACKOFF_SECONDS = (30, 120, 600, 1800, 3600, 3 * 3600)
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1

Handler = Callable[[dict[str, Any]], Any]


class Retry(Exception):
    """Delivery could not happen right now (no route, phone asleep); try again later."""


class Outbox:
    def __init__(self, app: "Felix") -> None:
        self.app = app
        self._handlers: dict[str, Handler] = {}
        self._rehearsals: dict[str, Handler] = {}
        self._flush_lock = threading.Lock()
        self.recover()
        app.bus.on("net.changed", lambda topic, data: data.get("online") and self.flush_async())
        app.bus.on("phone.connected", lambda topic, data: self.flush_async())

    def handler(self, kind: str, fn: Handler, rehearse: Handler | None = None) -> None:
        """Register how ``kind`` is delivered. ``rehearse`` records the local effects of a
        delivery without transmitting anything; the demo sandbox calls it instead of ``fn``."""
        self._handlers[kind] = fn
        if rehearse is not None:
            self._rehearsals[kind] = rehearse

    def enqueue(self, kind: str, payload: dict[str, Any], summary: str, approval_id: int | None = None,
                send_now: bool = True) -> dict[str, Any]:
        """Queue an item and try to deliver it immediately.

        Returns the outbox row; ``status`` is 'sent' or 'queued'.
        """
        if kind not in self._handlers:
            raise ToolError(f"Nothing is configured to deliver {kind}.")
        now = self.app.clock.ts()
        item_id = self.app.store.insert("outbox", {
            "kind": kind, "payload": payload, "summary": clip(summary, 200), "status": "queued",
            "created_at": now, "next_try": now, "approval_id": approval_id,
        })
        if send_now:
            self._deliver(self.get(item_id))  # type: ignore[arg-type]
        return self.get(item_id)  # type: ignore[return-value]

    def get(self, item_id: int) -> dict[str, Any] | None:
        return self.app.store.one("SELECT * FROM outbox WHERE id = ?", (item_id,))

    def pending(self) -> list[dict[str, Any]]:
        return self.app.store.query("SELECT * FROM outbox WHERE status IN ('queued', 'sending') ORDER BY id")

    def recover(self) -> int:
        """After a restart, anything caught mid-send goes back in the queue."""
        return self.app.store.update("outbox", {"status": "queued"}, "status = 'sending'")

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.app.store.query("SELECT * FROM outbox ORDER BY id DESC LIMIT ?", (limit,))

    def cancel(self, item_id: int) -> bool:
        return bool(self.app.store.update("outbox", {"status": "cancelled"}, "id = ? AND status = 'queued'", (item_id,)))

    def retry(self, item_id: int) -> dict[str, Any] | None:
        """Owner asked to try a failed item again."""
        self.app.store.update("outbox", {"status": "queued", "attempts": 0, "next_try": self.app.clock.ts(),
                                         "last_error": None}, "id = ? AND status = 'failed'", (item_id,))
        row = self.get(item_id)
        if row and row["status"] == "queued":
            self._deliver(row)
        return self.get(item_id)

    def flush(self) -> dict[str, int]:
        """Deliver everything that is due. Safe to call from any thread."""
        counts = {"sent": 0, "failed": 0, "waiting": 0}
        if not self._flush_lock.acquire(blocking=False):
            return counts
        try:
            due = self.app.store.query(
                "SELECT * FROM outbox WHERE status = 'queued' AND next_try <= ? ORDER BY id", (self.app.clock.ts(),))
            for row in due:
                outcome = self._deliver(row)
                counts[outcome] += 1
        finally:
            self._flush_lock.release()
        return counts

    def flush_async(self) -> None:
        threading.Thread(target=self._safe_flush, name="felix-outbox", daemon=True).start()

    def _safe_flush(self) -> None:
        try:
            self.flush()
        except Exception:
            log.exception("outbox flush failed")

    def _deliver(self, row: dict[str, Any]) -> str:
        handler = self._handlers.get(row["kind"])
        now = self.app.clock.ts()
        if handler is None:
            self.app.store.update("outbox", {"status": "failed", "last_error": "no handler"}, "id = ?", (row["id"],))
            return "failed"
        # Claim the item first. An approval and a reconnect can both try to deliver
        # it at the same instant; only one of them may actually send.
        if not self.app.store.update("outbox", {"status": "sending"}, "id = ? AND status = 'queued'", (row["id"],)):
            return "waiting"
        try:
            if self.app.sandbox:
                rehearse = self._rehearsals.get(row["kind"])
                if rehearse is not None:
                    rehearse(row["payload"])
                self.app.store.audit("felix", f"outbox.{row['kind']}", {"id": row["id"], "summary": row["summary"]},
                                     "demo: not sent")
            else:
                handler(row["payload"])
        except Retry as exc:
            # Not an error: the route is simply unavailable. The item stays due, so it
            # leaves the moment the connection (or the phone) comes back.
            self.app.store.update("outbox", {"status": "queued", "last_error": clip(str(exc), 300)},
                                  "id = ?", (row["id"],))
            return "waiting"
        except Exception as exc:
            attempts = (row["attempts"] or 0) + 1
            message = clip(str(exc) or exc.__class__.__name__, 300)
            if attempts >= MAX_ATTEMPTS:
                self.app.store.update("outbox", {"status": "failed", "attempts": attempts, "last_error": message},
                                      "id = ?", (row["id"],))
                self.app.store.audit("felix", f"outbox.{row['kind']}", {"id": row["id"], "error": message}, "failed")
                self.app.bus.publish("outbox.failed", {"id": row["id"], "kind": row["kind"],
                                                       "summary": row["summary"], "error": message})
                return "failed"
            delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
            self.app.store.update("outbox", {"status": "queued", "attempts": attempts, "last_error": message,
                                             "next_try": now + delay}, "id = ?", (row["id"],))
            log.warning("outbox item %s (%s) failed, retry in %ss: %s", row["id"], row["kind"], delay, message)
            return "waiting"
        self.app.store.update("outbox", {"status": "sent", "sent_at": now, "last_error": None}, "id = ?", (row["id"],))
        self.app.bus.publish("outbox.sent", {"id": row["id"], "kind": row["kind"], "summary": row["summary"]})
        return "sent"
