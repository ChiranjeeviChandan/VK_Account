"""The brain's front door: decide who answers, keep the conversation, report back.

Order of business for every message from the owner:

1. A plain "yes" / "no" to something awaiting approval is handled by code, not
   by a model - approval is the owner's act alone.
2. Unambiguous instant commands ("lock", "flashlight on", "go offline") run
   straight away, with or without internet.
3. Otherwise Claude answers when Felix is online, a local model when one is
   running, and the offline command engine when neither is available. If an
   engine fails mid-flight, the next one down takes over and says so.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Callable

from ..policy import public_approval
from ..tools import ToolContext
from .claude import ClaudeEngine, EngineError
from .local import LocalEngine
from .prompts import context_block, recap_from
from .rules import Rules

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix

log = logging.getLogger("felix.brain")

Emit = Callable[[dict[str, Any]], None]

IDLE_SECONDS = 3 * 3600          # a conversation left this long starts fresh (with a short recap)
MAX_INPUT_TOKENS = 150_000       # and so does one that has grown this large


class Brain:
    def __init__(self, app: "Felix") -> None:
        self.app = app
        self.rules = Rules(app)
        self.claude = ClaudeEngine(app)
        self.local = LocalEngine(app)
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    # -- which engine ------------------------------------------------------------
    def _engines(self) -> list[Any]:
        engines: list[Any] = []
        if self.app.net.online and self.claude.available():
            engines.append(self.claude)
        if self.local.available():
            engines.append(self.local)
        return engines

    def engine_name(self) -> str:
        """Who would answer a free-form question right now."""
        engines = self._engines()
        return engines[0].name if engines else "rules"

    def warm_up(self) -> threading.Thread:
        """Do the slow first-time work in the background, before the first question
        rather than during it: load the Claude SDK and look for a local model."""
        def work() -> None:
            try:
                if self.claude.available():
                    self.claude.client()
                self.local.available()
            except Exception:             # only a head start: the first question would do the same
                log.debug("warm-up did not finish", exc_info=True)

        thread = threading.Thread(target=work, name="felix-warm-up", daemon=True)
        thread.start()
        return thread

    def describe(self) -> dict[str, Any]:
        return {
            "engine": self.engine_name(), "model": self.claude.model(),
            "claude_ready": self.claude.available(), "local_ready": self.local.available(),
            "local_model": self.local.config().get("model"),
        }

    # -- answering ---------------------------------------------------------------
    def respond(self, text: str, thread: str = "main", source: str = "ui", emit: Emit | None = None) -> dict[str, Any]:
        """Answer one message from the owner. Never raises."""
        text = (text or "").strip()
        send: Emit = emit or (lambda event: None)
        if not text:
            return self._result("Yes?", "rules", [], [])
        with self._lock_for(thread):
            before = int(self.app.store.scalar("SELECT COALESCE(MAX(id), 0) FROM approvals", default=0))
            try:
                reply, engine, notes, model = self._answer(text, thread, source, send)
            except Exception as exc:      # the owner always gets an answer, never a stack trace
                log.exception("brain failed on a message")
                reply, engine, notes, model = f"Something went wrong inside Felix: {exc}", "rules", [], None
                send({"type": "delta", "text": reply})
            approvals = [public_approval(row) for row in self.app.store.query(
                "SELECT * FROM approvals WHERE id > ? AND thread = ? AND status = 'pending' ORDER BY id", (before, thread))]
            if approvals and engine in ("claude", "local"):
                # A model described what it asked for; the owner must also see the request itself,
                # exactly as stored: the recipient, the text, the command. Code writes this part.
                block = waiting_block(approvals)
                reply = f"{reply.rstrip()}\n\n{block}" if reply.strip() else block
                send({"type": "delta", "text": "\n\n" + block})
                self._extend_last_reply(thread, "\n\n" + block)
            return self._result(reply, engine, approvals, notes, model)

    def _extend_last_reply(self, thread: str, addition: str) -> None:
        """Keep the transcript in step with what the owner was shown."""
        last = self.app.store.scalar(
            "SELECT MAX(t.id) FROM turns t JOIN conversations c ON c.id = t.conversation_id "
            "WHERE c.thread = ? AND t.role = 'assistant'", (thread,))
        if last:
            self.app.store.execute("UPDATE turns SET text = COALESCE(text, '') || ? WHERE id = ?", (addition, last))

    def _answer(self, text: str, thread: str, source: str, emit: Emit) -> tuple[str, str, list[str], str | None]:
        owner = ToolContext(self.app, thread=thread, source=source, actor="owner", engine="rules")

        decided = self.app.policy.handle_reply(text, thread, via=source)
        if decided is not None:
            return self._say(thread, text, decided, "policy", emit), "policy", [], None

        if self.app.config.get("brain.fast_commands", True):
            instant = self.rules.respond(text, owner, fast_only=True)
            if instant is not None:
                return self._say(thread, text, instant, "rules", emit), "rules", [], None

        notes: list[str] = []
        for engine in self._engines():
            try:
                result = self._converse(engine, thread, text, source, emit)
                return result["text"], engine.name, notes, result.get("model")
            except EngineError as exc:
                log.warning("%s unavailable: %s", engine.name, exc.reason)
                notes.append(exc.reason)
                if exc.kind == "network":
                    self.app.net.reachable(force=True)

        answer = self.rules.respond(text, owner)
        if answer is None:
            answer = self.rules.unknown(text, owner)
        if notes:
            answer = f"({notes[0]} Answering with offline commands.)\n\n{answer}"
        return self._say(thread, text, answer, "rules", emit), "rules", notes, None

    def _say(self, thread: str, text: str, reply: str, engine: str, emit: Emit) -> str:
        """Record an exchange that no model took part in, and stream it in one piece."""
        conversation = self._conversation(thread, "rules")[0]
        now = self.app.clock.ts()
        self.app.store.insert("turns", {"conversation_id": conversation["id"], "role": "user", "text": text, "ts": now,
                                        "native": [], "meta": {"engine": engine}})
        self.app.store.insert("turns", {"conversation_id": conversation["id"], "role": "assistant", "text": reply,
                                        "ts": now, "native": [], "meta": {"engine": engine}})
        self.app.store.update("conversations", {"updated_at": now}, "id = ?", (conversation["id"],))
        emit({"type": "delta", "text": reply})
        return reply

    def _converse(self, engine: Any, thread: str, text: str, source: str, emit: Emit, retried: bool = False) -> dict[str, Any]:
        conversation, recap = self._conversation(thread, engine.name)
        history = self._history(conversation["id"])
        since = self._since(thread, conversation["id"])
        context = context_block(self.app, thread, recap or since)
        if engine.name == "claude":
            message: dict[str, Any] = {"role": "user", "content": [{"type": "text", "text": context},
                                                                   {"type": "text", "text": text}]}
        else:
            message = {"role": "user", "content": f"{context}\n\n{text}"}
        ctx = ToolContext(self.app, thread=thread, source=source, actor="felix", engine=engine.name)
        emit({"type": "engine", "engine": engine.name})
        try:
            result = engine.run(history + [message], ctx, emit)
        except EngineError as exc:
            if exc.kind == "history" and history and not retried:
                # The stored conversation is no longer acceptable (for example the model
                # was changed). Retire it and answer from a fresh one.
                self.app.store.update("conversations", {"closed": 1}, "id = ?", (conversation["id"],))
                return self._converse(engine, thread, text, source, emit, retried=True)
            raise
        now = self.app.clock.ts()
        self.app.store.insert("turns", {"conversation_id": conversation["id"], "role": "user", "text": text, "ts": now,
                                        "native": [message], "meta": {"engine": engine.name}})
        self.app.store.insert("turns", {"conversation_id": conversation["id"], "role": "assistant",
                                        "text": result["text"], "ts": now, "native": result["messages"],
                                        "meta": {"engine": engine.name, "model": result.get("model")}})
        full = result["stop"] == "refusal" or result["input_tokens"] > MAX_INPUT_TOKENS
        self.app.store.update("conversations", {"updated_at": now, "input_tokens": result["input_tokens"],
                                                "closed": int(full)}, "id = ?", (conversation["id"],))
        return result

    # -- conversations -----------------------------------------------------------
    def _lock_for(self, thread: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(thread, threading.RLock())

    def _conversation(self, thread: str, engine: str) -> tuple[dict[str, Any], str]:
        """The open conversation for this thread and engine, or a new one plus a recap."""
        now = self.app.clock.ts()
        row = self.app.store.one(
            "SELECT * FROM conversations WHERE thread = ? AND engine = ? AND closed = 0 ORDER BY id DESC LIMIT 1",
            (thread, engine))
        if row is not None and now - (row["updated_at"] or 0) <= IDLE_SECONDS:
            return row, ""
        if row is not None:
            self.app.store.update("conversations", {"closed": 1}, "id = ?", (row["id"],))
        recap = ""
        if engine != "rules":
            recent = [t for t in self.transcript(thread, 10) if now - (t["ts"] or 0) < 24 * 3600]
            recap = recap_from(recent)
        conversation_id = self.app.store.insert("conversations", {
            "thread": thread, "engine": engine, "created_at": now, "updated_at": now, "closed": 0, "input_tokens": 0})
        return self.app.store.one("SELECT * FROM conversations WHERE id = ?", (conversation_id,)), recap  # type: ignore[return-value]

    def _history(self, conversation_id: int) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for turn in self.app.store.query("SELECT native FROM turns WHERE conversation_id = ? ORDER BY id", (conversation_id,)):
            native = turn["native"]
            if isinstance(native, list):
                messages += native
        return messages

    def _since(self, thread: str, conversation_id: int) -> str:
        """What happened in this thread outside this conversation since its last turn
        (approvals the owner gave, instant commands) - so the model is not surprised."""
        last = int(self.app.store.scalar("SELECT COALESCE(MAX(id), 0) FROM turns WHERE conversation_id = ?",
                                         (conversation_id,), 0))
        if not last:
            return ""
        rows = self.app.store.query(
            "SELECT t.role, t.text, t.ts FROM turns t JOIN conversations c ON c.id = t.conversation_id "
            "WHERE c.thread = ? AND t.id > ? AND t.conversation_id != ? ORDER BY t.id", (thread, last, conversation_id))
        return recap_from(rows, limit=6)

    def transcript(self, thread: str = "main", limit: int = 60) -> list[dict[str, Any]]:
        rows = self.app.store.query(
            "SELECT t.id, t.role, t.text, t.ts, t.meta FROM turns t JOIN conversations c ON c.id = t.conversation_id "
            "WHERE c.thread = ? ORDER BY t.id DESC LIMIT ?", (thread, limit))
        return [{"id": r["id"], "role": r["role"], "text": r["text"], "ts": r["ts"],
                 "engine": (r["meta"] or {}).get("engine") if isinstance(r["meta"], dict) else None}
                for r in reversed(rows)]

    def reset(self, thread: str = "main") -> None:
        """Forget the running conversation (the transcript stays in the log)."""
        self.app.store.update("conversations", {"closed": 1}, "thread = ? AND closed = 0", (thread,))

    def _result(self, reply: str, engine: str, approvals: list[dict[str, Any]], notes: list[str],
                model: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"reply": reply, "engine": engine, "online": self.app.net.online, "approvals": approvals}
        if notes:
            out["notes"] = notes
        if model:
            out["model"] = model
        return out


def waiting_block(approvals: list[dict[str, Any]]) -> str:
    """What is waiting for the owner, word for word as it will be carried out."""
    parts = [f"Waiting for your go-ahead (#{a['id']}):\n{a.get('preview') or a.get('tool', '')}" for a in approvals]
    if len(approvals) == 1:
        ending = 'Reply "yes" to go ahead or "no" to cancel.'
    else:
        ending = 'Reply "approve <number>", "approve all" or "no".'
    return "\n\n".join(parts + [ending])
