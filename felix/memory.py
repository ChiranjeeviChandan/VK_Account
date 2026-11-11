"""Long-term memory (facts the owner asked Felix to keep) and global search."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .tools import LOCAL, READ, Registry, ToolContext, ToolError
from .util import clip

if TYPE_CHECKING:  # pragma: no cover
    from .app import Felix


class Memory:
    def __init__(self, app: "Felix") -> None:
        self.app = app

    def remember(self, text: str, tags: str | None = None) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            raise ToolError("There is nothing to remember.")
        existing = self.app.store.one("SELECT * FROM notes WHERE text = ?", (text,))
        if existing:
            return existing
        now = self.app.clock.ts()
        note_id = self.app.store.insert("notes", {"text": text, "tags": tags, "created_at": now, "updated_at": now})
        self.app.store.index("note", note_id, clip(text, 80), text, tags or "", now)
        return self.app.store.one("SELECT * FROM notes WHERE id = ?", (note_id,))  # type: ignore[return-value]

    def forget(self, note_id: int) -> bool:
        removed = self.app.store.delete("notes", "id = ?", (note_id,))
        self.app.store.unindex("note", note_id)
        return bool(removed)

    def all(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.app.store.query("SELECT * FROM notes ORDER BY updated_at DESC LIMIT ?", (limit,))

    def recall(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        hits = self.app.store.search(query, kinds=["note"], limit=limit)
        ids = [h["ref"] for h in hits]
        if not ids:
            return []
        rows = {r["id"]: r for r in self.app.store.query(
            f"SELECT * FROM notes WHERE id IN ({', '.join('?' for _ in ids)})", ids)}
        return [rows[i] for i in ids if i in rows]

    def context_block(self, limit: int = 25, max_chars: int = 2500) -> str:
        """Standing facts handed to the assistant with every request."""
        lines: list[str] = []
        used = 0
        for note in self.all(limit):
            line = f"- {clip(note['text'], 300)}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

    def register(self, tools: Registry) -> None:
        @tools.tool(
            "memory_save",
            "Remember a lasting fact or preference for future conversations (for example 'my assistant is Priya', "
            "'never book meetings before 9', 'the board meets on the first Monday'). "
            "Call it when the owner says 'remember that...' or states a standing preference.",
            {"text": {"type": "string"}, "tags": {"type": "string", "description": "Optional keywords"}},
            ["text"], risk=LOCAL, group="memory",
        )
        def memory_save(args: dict[str, Any], ctx: ToolContext) -> Any:
            note = self.remember(args["text"], args.get("tags"))
            return {"message": f"I'll remember that (note #{note['id']}).", "id": note["id"]}

        @tools.tool(
            "memory_search",
            "Search the facts Felix was asked to remember.",
            {"query": {"type": "string"}},
            ["query"], risk=READ, group="memory",
        )
        def memory_search(args: dict[str, Any], ctx: ToolContext) -> Any:
            return {"notes": [{"id": n["id"], "text": n["text"]} for n in self.recall(args["query"])]}

        @tools.tool(
            "memory_delete",
            "Forget a remembered fact by its note number.",
            {"id": {"type": "integer"}},
            ["id"], risk=LOCAL, group="memory",
        )
        def memory_delete(args: dict[str, Any], ctx: ToolContext) -> Any:
            if not self.forget(args["id"]):
                raise ToolError(f"There is no note #{args['id']}.")
            return {"message": f"Forgot note #{args['id']}."}

        @tools.tool(
            "search_everything",
            "Full-text search across everything stored locally: emails, chat messages, calendar events, "
            "tasks and remembered notes. Works offline. Use it to find 'that email about the lease' or "
            "'what did Ravi say about pricing'.",
            {
                "query": {"type": "string"},
                "kinds": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["email", "message", "event", "task", "note"]},
                    "description": "Limit to these kinds; default all",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["query"], risk=READ, group="memory",
        )
        def search_everything(args: dict[str, Any], ctx: ToolContext) -> Any:
            hits = self.app.store.search(args["query"], kinds=args.get("kinds"), limit=args.get("limit", 15))
            out = []
            for hit in hits:
                item = {"kind": hit["kind"], "id": hit["ref"], "title": hit["title"], "excerpt": hit["excerpt"]}
                if hit["who"]:
                    item["who"] = hit["who"]
                if hit["ts"]:
                    item["when"] = self.app.clock.from_ts(hit["ts"]).isoformat(timespec="minutes")
                out.append(item)
            return {"results": out, "count": len(out)}
