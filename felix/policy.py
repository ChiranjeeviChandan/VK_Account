"""Approval policy: what Felix may do alone and what waits for the owner.

Design rule: an approval can only be granted through an owner-authenticated
surface (dashboard, CLI, paired phone, the owner's own typed "yes"). It is
never exposed as a tool, so text inside an email or chat message - or a model
that was talked into something - cannot approve its own request.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .config import DANGER_CHANNELS
from .tools import DANGER, LOCAL, OUTBOUND, READ, Tool, ToolContext, ToolResult

if TYPE_CHECKING:  # pragma: no cover
    from .app import Felix

log = logging.getLogger("felix.policy")

# Where an approval can come from other than the laptop itself.
REMOTE_SURFACES = frozenset({"phone", "whatsapp"})
# Approving these changes the laptop itself, so by default it has to happen at the laptop.
LAPTOP_ONLY_CHANNELS = frozenset(DANGER_CHANNELS | {"files_write"})

_FILL = r"(?:[\s,.!]+|\s*\b(?:please|pls|thanks|thank\s+you|felix)\b\s*)"
_TARGET = r"(?:#?(\d+)|all|both|them|it|that|this|everything)"
_AFFIRM = (
    r"(?:yes|y|yep|yeah|yup|ok|okay|sure|approved?|confirm(?:ed)?|send(?:\s+(?:it|that|them))?|go\s+ahead|"
    r"do\s+(?:it|that)|proceed|go|haan|ha|theek\s+hai|kar\s+do|bhej\s+do)"
)
_NEGATE = (
    r"(?:no|n|nope|nah|cancel|deny|denied|reject|discard|stop|drop\s+it|never\s*mind|nahi|mat\s+karo|mat\s+bhejo|"
    r"(?:don'?t|do\s+not)(?:\s+(?:send|do))?(?:\s+(?:it|that|them))?)"
)
# The whole message must be nothing but agreement (or refusal): "ok, what about
# tomorrow?" is a question for the assistant, not an approval.
_YES = re.compile(rf"^{_FILL}*{_AFFIRM}(?:{_FILL}+{_AFFIRM})*{_FILL}*(?:{_TARGET}{_FILL}*)?$", re.I)
_NO = re.compile(rf"^{_FILL}*{_NEGATE}(?:{_FILL}+{_NEGATE})*{_FILL}*(?:{_TARGET}{_FILL}*)?$", re.I)
_ALL = re.compile(r"\b(?:all|both|them|everything)\b", re.I)


@dataclass
class Decision:
    action: str          # allow | ask | deny
    reason: str = ""


class Policy:
    def __init__(self, app: "Felix") -> None:
        self.app = app
        self._lock = threading.RLock()

    # -- deciding ------------------------------------------------------------
    def decide(self, tool: Tool, args: dict[str, Any], ctx: ToolContext) -> Decision:
        risk = tool.risk_for(args)
        if risk == READ:
            return Decision("allow")
        if ctx.preapproved:
            return Decision("allow", "approved by owner")
        channel = tool.channel_for(args)
        if risk == LOCAL:
            # Harmless by default, but the owner can still demand to be asked
            # (for example before any calendar change) or switch a channel off.
            autonomy = self.app.config.get(f"policy.autonomy.{channel}", "auto") if channel else "auto"
            if autonomy == "never":
                return Decision("deny", f"Your policy never allows {channel.replace('_', ' ')} actions.")
            return Decision("ask" if autonomy == "ask" else "allow")
        channel = channel or ("shell" if risk == DANGER else "email")
        autonomy = self.app.config.get(f"policy.autonomy.{channel}", "ask")
        if autonomy == "never":
            return Decision("deny", f"Your policy never allows {channel.replace('_', ' ')} actions.")
        if risk == OUTBOUND and tool.recipients is not None:
            try:
                recipients = [r for r in tool.recipients(args, ctx) if r]
            except Exception:
                recipients = []
            if recipients and self._all_self(recipients):
                return Decision("allow", "message to the owner")
            if recipients and self._all_trusted(recipients):
                return Decision("allow", "trusted recipient")
        if autonomy == "auto":
            if channel in DANGER_CHANNELS and not self.app.config.get("policy.allow_unattended_danger", False):
                return Decision("ask", "dangerous actions always need approval")
            return Decision("allow", f"policy allows {channel} without asking")
        return Decision("ask", "needs owner approval")

    def _all_self(self, recipients: list[str]) -> bool:
        own = {_norm(self.app.config.get("owner.email", "")), _norm(self.app.config.get("owner.phone", ""))}
        for account in self.app.config.get("mail.accounts", []) or []:
            own.add(_norm(account.get("email", "")))
        own.discard("")
        return bool(own) and all(any(_same(_norm(r), mine) for mine in own) for r in recipients)

    def _all_trusted(self, recipients: list[str]) -> bool:
        trusted = {_norm(r) for r in self.app.config.get("policy.trusted_recipients", []) or []}
        trusted.discard("")
        return bool(trusted) and all(any(_same(_norm(r), known) for known in trusted) for r in recipients)

    # -- requesting ----------------------------------------------------------
    def request(self, tool: Tool, args: dict[str, Any], ctx: ToolContext, preview: str, risk: str) -> dict[str, Any]:
        now = self.app.clock.ts()
        ttl = float(self.app.config.get("policy.approval_ttl_minutes", 720)) * 60
        with self._lock:
            # A model that retries the identical call must not stack duplicates.
            for row in self.pending(thread=ctx.thread):
                if row["tool"] == tool.name and row["args"] == args:
                    return row
            approval_id = self.app.store.insert("approvals", {
                "tool": tool.name, "args": args, "preview": preview, "risk": risk,
                "channel": tool.channel_for(args), "status": "pending", "thread": ctx.thread,
                "source": ctx.source, "created_at": now, "expires_at": now + ttl,
            })
        approval = self.get(approval_id)
        assert approval is not None
        self.app.store.audit(ctx.actor, "approval.requested", {"id": approval_id, "tool": tool.name}, "pending")
        self.app.bus.publish("approval.created", _public(approval))
        return approval

    # -- reading -------------------------------------------------------------
    def get(self, approval_id: int) -> dict[str, Any] | None:
        return self.app.store.one("SELECT * FROM approvals WHERE id = ?", (approval_id,))

    def pending(self, thread: str | None = None) -> list[dict[str, Any]]:
        self.expire()
        if thread is None:
            return self.app.store.query("SELECT * FROM approvals WHERE status = 'pending' ORDER BY id")
        return self.app.store.query(
            "SELECT * FROM approvals WHERE status = 'pending' AND thread = ? ORDER BY id", (thread,)
        )

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        self.expire()
        return self.app.store.query("SELECT * FROM approvals ORDER BY id DESC LIMIT ?", (limit,))

    def expire(self) -> int:
        return self.app.store.update(
            "approvals", {"status": "expired"}, "status = 'pending' AND expires_at < ?", (self.app.clock.ts(),)
        )

    # -- deciding (owner only) -------------------------------------------------
    def approve(self, approval_id: int, via: str, args: dict[str, Any] | None = None) -> ToolResult:
        """Run a pending action. ``args`` lets the owner edit before sending."""
        with self._lock:
            row = self.get(approval_id)
            if row is None:
                return ToolResult(False, error=f"No approval #{approval_id}.")
            if row["status"] != "pending":
                return ToolResult(False, error=f"Approval #{approval_id} is already {row['status']}.")
            if row["expires_at"] and row["expires_at"] < self.app.clock.ts():
                self.app.store.update("approvals", {"status": "expired"}, "id = ?", (approval_id,))
                return ToolResult(False, error=f"Approval #{approval_id} has expired.")
            if (row["channel"] in LAPTOP_ONLY_CHANNELS and via in REMOTE_SURFACES
                    and not self.app.config.get("policy.remote_danger_approval", False)):
                # Stays pending: the owner can still approve it at the laptop.
                self.app.store.audit("owner", "approval.refused", {"id": approval_id, "tool": row["tool"], "via": via},
                                     "laptop only")
                return ToolResult(False, error=(
                    f"#{approval_id} changes your laptop, so it can only be approved on the laptop itself "
                    f"(the dashboard, or: felix approve {approval_id})."))
            final_args = args if args is not None else row["args"]
            self.app.store.update(
                "approvals",
                {"status": "approved", "decided_at": self.app.clock.ts(), "decided_via": via, "args": final_args},
                "id = ?", (approval_id,),
            )
        self.app.store.audit("owner", "approval.approved", {"id": approval_id, "tool": row["tool"], "via": via,
                                                            "edited": args is not None})
        ctx = ToolContext(self.app, thread=row["thread"] or "main", source=via).approved(approval_id)
        result = self.app.tools.execute(row["tool"], final_args, ctx)
        status = "executed" if result.ok else "failed"
        self.app.store.update(
            "approvals",
            {"status": status, "result": {"ok": result.ok, "data": result.data, "error": result.error}},
            "id = ?", (approval_id,),
        )
        self.app.bus.publish("approval.resolved", {"id": approval_id, "status": status, "tool": row["tool"]})
        return result

    def deny(self, approval_id: int, via: str) -> ToolResult:
        with self._lock:
            row = self.get(approval_id)
            if row is None:
                return ToolResult(False, error=f"No approval #{approval_id}.")
            if row["status"] != "pending":
                return ToolResult(False, error=f"Approval #{approval_id} is already {row['status']}.")
            self.app.store.update(
                "approvals", {"status": "denied", "decided_at": self.app.clock.ts(), "decided_via": via},
                "id = ?", (approval_id,),
            )
        self.app.store.audit("owner", "approval.denied", {"id": approval_id, "tool": row["tool"]}, "denied")
        self.app.bus.publish("approval.resolved", {"id": approval_id, "status": "denied", "tool": row["tool"]})
        return ToolResult(True, data={"message": f"Cancelled #{approval_id}."})

    # -- the owner answering "yes" / "no" in a conversation ------------------
    def handle_reply(self, text: str, thread: str, via: str) -> str | None:
        """If ``text`` is a plain yes/no to something waiting in this thread,
        act on it and return the message to show. Otherwise return None so the
        message goes to the assistant as usual.

        This runs on the owner's raw input before any model sees it.
        """
        yes, no = _YES.match(text or ""), _NO.match(text or "")
        if not yes and not no:
            return None
        waiting = self.pending(thread=thread)
        if not waiting:
            return None
        match = yes or no
        assert match is not None
        wanted_id = int(match.group(1)) if match.group(1) else None
        if wanted_id is not None:
            targets = [row for row in waiting if row["id"] == wanted_id]
            if not targets:
                other = self.get(wanted_id)
                if other and other["status"] == "pending":
                    targets = [other]   # named explicitly, so the thread need not match
                else:
                    return f"There is no pending approval #{wanted_id}."
        elif _ALL.search(text) or len(waiting) == 1:
            targets = waiting
        else:
            listing = "\n".join(f"  #{row['id']}  {_headline(row)}" for row in waiting)
            verb = "approve" if yes else "cancel"
            return (f"{len(waiting)} things are waiting. Say \"{verb} <number>\" or \"{verb} all\":\n{listing}")

        lines: list[str] = []
        for row in targets:
            if yes:
                result = self.approve(row["id"], via)
                if result.ok:
                    lines.append(f"Done - {_headline(row)}." if not _message(result) else _message(result))
                else:
                    lines.append(f"#{row['id']} failed: {result.error}")
            else:
                self.deny(row["id"], via)
                lines.append(f"Cancelled - {_headline(row)}.")
        return "\n".join(lines)


def _message(result: ToolResult) -> str:
    if isinstance(result.data, dict) and isinstance(result.data.get("message"), str):
        return result.data["message"]
    return result.data if isinstance(result.data, str) else ""


def _headline(row: dict[str, Any]) -> str:
    preview = (row.get("preview") or row.get("tool") or "").strip().splitlines()
    head = preview[0] if preview else row.get("tool", "action")
    extra = next((line for line in preview[1:] if line.strip()), "")
    return f"{head} ({extra})" if extra and len(extra) < 80 else head


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: row.get(k) for k in ("id", "tool", "preview", "risk", "channel", "status", "thread", "created_at", "expires_at")}


def public_approval(row: dict[str, Any]) -> dict[str, Any]:
    return _public(row)


def _norm(value: str) -> str:
    """A comparison key for an address or number. Deliberately strict: an address that only
    *looks* like the owner's (an accent, another country code) is somebody else's."""
    value = (value or "").strip()
    if "@" in value:
        local, _, domain = value.rpartition("@")
        try:
            domain = domain.encode("idna").decode("ascii")     # an accented domain is a different domain
        except UnicodeError:
            pass
        return f"{local}@{domain}".casefold()
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 7:
        return ("+" if value.startswith("+") or len(digits) > 10 else "") + digits
    return value.casefold()


def _same(candidate: str, reference: str) -> bool:
    """True when ``candidate`` names the mailbox or phone ``reference`` (keys from :func:`_norm`)."""
    if candidate == reference:
        return True
    if "@" in candidate or "@" in reference:
        return False
    # A contact saved without its country code matches the full number it is the tail of.
    # Never the other way round, and never two different country codes.
    return candidate.isdigit() and len(candidate) == 10 and reference.lstrip("+").isdigit() \
        and reference.lstrip("+").endswith(candidate)
