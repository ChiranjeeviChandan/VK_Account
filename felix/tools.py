"""Tool registry: the single doorway through which every engine acts.

Claude, a local model and the offline command engine all call the same tools.
The registry validates input, asks the policy whether the action may run now,
executes it, and writes the audit trail. No engine can bypass it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable

from .util import clip, json_dumps

if TYPE_CHECKING:  # pragma: no cover
    from .app import Felix

log = logging.getLogger("felix.tools")

READ = "read"          # looks things up; never changes anything
LOCAL = "local"        # changes Felix's own data or harmless device state
OUTBOUND = "outbound"  # speaks for the owner to someone else
DANGER = "danger"      # hard to undo on the laptop or phone


class ToolError(Exception):
    """An expected failure with a message that is safe to show."""


@dataclass
class ToolContext:
    app: "Felix"
    thread: str = "main"          # which conversation surface asked
    source: str = "ui"            # ui | cli | phone | whatsapp | overlay | scheduler | system
    actor: str = "felix"          # felix (a model decided) | owner (typed verbatim) | scheduler
    engine: str = ""
    preapproved: bool = False     # True only while executing an owner-approved action
    approval_id: int | None = None

    def approved(self, approval_id: int) -> "ToolContext":
        return replace(self, preapproved=True, approval_id=approval_id, actor="owner")

    def direct(self) -> "ToolContext":
        """The owner typed this exact action themselves (a command, a form in the dashboard):
        their own words are the approval. Never use it for something a model decided."""
        return replace(self, preapproved=True, actor="owner")


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    pending: dict[str, Any] | None = None   # set when the action awaits approval

    def for_model(self) -> str:
        if self.pending:
            return json_dumps({
                "status": "awaiting_owner_approval",
                "approval_id": self.pending["id"],
                "note": "Not done yet. The owner must approve this in Felix; you cannot approve it. "
                        "Tell them briefly what is waiting.",
                "preview": self.pending.get("preview"),
            })
        if not self.ok:
            return json_dumps({"error": self.error})
        return self.data if isinstance(self.data, str) else json_dumps(self.data)

    def for_human(self) -> str:
        if self.pending:
            return f"Waiting for your approval (#{self.pending['id']}):\n{self.pending.get('preview', '')}"
        if not self.ok:
            return f"Could not do that: {self.error}"
        if isinstance(self.data, str):
            return self.data
        if isinstance(self.data, dict) and isinstance(self.data.get("message"), str):
            return self.data["message"]
        return json_dumps(self.data, indent=2)


@dataclass
class Tool:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any], ToolContext], Any]
    risk: str = READ
    channel: str | Callable[[dict[str, Any]], str | None] | None = None
    preview: Callable[[dict[str, Any], ToolContext], str] | None = None
    recipients: Callable[[dict[str, Any], ToolContext], list[str]] | None = None
    needs_network: bool = False
    group: str = "general"
    extra: dict[str, Any] = field(default_factory=dict)

    def channel_for(self, args: dict[str, Any]) -> str | None:
        return self.channel(args) if callable(self.channel) else self.channel

    def risk_for(self, args: dict[str, Any]) -> str:
        override = self.extra.get("risk_for")
        return override(args) if callable(override) else self.risk


class Registry:
    def __init__(self, app: "Felix") -> None:
        self.app = app
        self._tools: dict[str, Tool] = {}

    # -- registration ------------------------------------------------------
    def add(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        schema = tool.schema
        if schema.get("type") != "object":
            raise ValueError(f"{tool.name}: schema must describe an object")
        schema.setdefault("properties", {})
        schema.setdefault("additionalProperties", False)
        self._tools[tool.name] = tool
        return tool

    def tool(self, name: str, description: str, properties: dict[str, Any] | None = None,
             required: list[str] | None = None, **kwargs: Any):
        """Decorator form of :meth:`add`."""
        def wrap(handler: Callable[[dict[str, Any], ToolContext], Any]) -> Callable:
            self.add(Tool(
                name=name, description=description, handler=handler,
                schema={"type": "object", "properties": properties or {}, "required": required or []},
                **kwargs,
            ))
            return handler
        return wrap

    def get(self, name: str) -> Tool | None:
        tool = self._tools.get(name)
        if tool is None:
            folded = {k.lower(): v for k, v in self._tools.items()}
            tool = folded.get((name or "").lower())
        return tool

    def all(self) -> list[Tool]:
        return [self._tools[k] for k in sorted(self._tools)]

    def names(self) -> list[str]:
        return sorted(self._tools)

    # -- execution ---------------------------------------------------------
    def execute(self, name: str, args: dict[str, Any] | None, ctx: ToolContext) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(False, error=f"Unknown tool {name!r}. Available: {', '.join(self.names())}")
        if not isinstance(args, dict):
            return ToolResult(False, error=f"{tool.name}: input must be a JSON object")
        clean, problems = validate(args, tool.schema)
        if problems:
            return ToolResult(False, error=f"{tool.name}: invalid input - " + "; ".join(problems))

        # Pin down who or what this call is about (a contact name becomes an address, a chat
        # name becomes that chat) before anyone is shown a preview. What the owner approves is
        # then exactly what runs, whatever changes in between.
        freeze = tool.extra.get("freeze")
        if callable(freeze):
            try:
                clean = freeze(clean, ctx)
            except ToolError as exc:
                return ToolResult(False, error=str(exc))

        risk = tool.risk_for(clean)
        policy = self.app.policy
        decision = policy.decide(tool, clean, ctx)
        if decision.action == "deny":
            self._audit(ctx, tool, clean, "denied", decision.reason)
            return ToolResult(False, error=decision.reason)
        if decision.action == "ask":
            try:
                preview = tool.preview(clean, ctx) if tool.preview else default_preview(tool, clean)
            except ToolError as exc:
                return ToolResult(False, error=str(exc))
            except Exception as exc:
                log.exception("preview for %s failed", tool.name)
                return ToolResult(False, error=f"{tool.name}: {exc}")
            approval = policy.request(tool, clean, ctx, preview, risk)
            return ToolResult(True, pending=approval)

        if tool.needs_network and not self.app.net.online and not tool.extra.get("queues_offline"):
            return ToolResult(False, error="Felix is offline, so this cannot run right now.")
        try:
            data = tool.handler(clean, ctx)
        except ToolError as exc:
            if risk != READ:
                self._audit(ctx, tool, clean, "failed", str(exc))
            return ToolResult(False, error=str(exc))
        except Exception as exc:  # a broken tool must not take the assistant down
            log.exception("tool %s crashed", tool.name)
            self._audit(ctx, tool, clean, "error", repr(exc))
            return ToolResult(False, error=f"{tool.name} failed: {exc}")
        if risk != READ:
            self._audit(ctx, tool, clean, "ok", None)
        return ToolResult(True, data=data)

    def _audit(self, ctx: ToolContext, tool: Tool, args: dict[str, Any], outcome: str, note: str | None) -> None:
        detail: dict[str, Any] = {"args": redact(args), "source": ctx.source, "engine": ctx.engine}
        if ctx.approval_id:
            detail["approval_id"] = ctx.approval_id
        if note:
            detail["note"] = clip(note, 300)
        try:
            self.app.store.audit(ctx.actor, tool.name, detail, outcome)
        except Exception:
            log.exception("audit write failed")


# -- helpers -----------------------------------------------------------------

_SECRET_HINTS = ("password", "token", "secret", "api_key", "apikey")


def redact(value: Any, depth: int = 0) -> Any:
    """Shrink and scrub arguments before they reach the audit log."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if any(h in str(key).lower() for h in _SECRET_HINTS):
                out[key] = "***"
            else:
                out[key] = redact(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, depth + 1) for v in list(value)[:20]]
    if isinstance(value, str):
        return clip(value, 400)
    return value


def default_preview(tool: Tool, args: dict[str, Any]) -> str:
    lines = [tool.name.replace("_", " ").capitalize()]
    for key, value in args.items():
        text = value if isinstance(value, str) else json_dumps(value)
        lines.append(f"{key}: {clip(text, 500)}")
    return "\n".join(lines)


_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
    "array": (list,), "object": (dict,), "null": (type(None),),
}


def validate(value: Any, schema: dict[str, Any], path: str = "") -> tuple[Any, list[str]]:
    """Check ``value`` against the JSON-Schema subset Felix tools use.

    Returns a cleaned copy (with harmless coercions such as "3" -> 3 applied,
    since smaller local models often quote numbers) and a list of problems.
    """
    problems: list[str] = []
    label = path or "input"
    types = schema.get("type")
    type_list = [types] if isinstance(types, str) else list(types or [])

    if value is None and ("null" in type_list or not type_list):
        return value, problems

    if type_list:
        value, matched = _coerce(value, type_list)
        if not matched:
            problems.append(f"{label} must be {' or '.join(type_list)}")
            return value, problems

    if "enum" in schema and value not in schema["enum"]:
        folded = {str(e).lower(): e for e in schema["enum"]}
        if isinstance(value, str) and value.lower() in folded:
            value = folded[value.lower()]
        else:
            problems.append(f"{label} must be one of {', '.join(map(str, schema['enum']))}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{label} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{label} is too long (max {schema['maxLength']} characters)")
    elif isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{label} must be at least {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{label} must be at most {schema['maximum']}")
    elif isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{label} needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{label} allows at most {schema['maxItems']} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            cleaned = []
            for index, item in enumerate(value):
                item, sub = validate(item, item_schema, f"{label}[{index}]")
                cleaned.append(item)
                problems += sub
            value = cleaned
    elif isinstance(value, dict):
        props: dict[str, Any] = schema.get("properties", {})
        cleaned_obj: dict[str, Any] = {}
        for key in schema.get("required", []):
            if key not in value or value[key] is None:
                problems.append(f"missing required field {path + '.' if path else ''}{key}")
        for key, item in value.items():
            if key in props:
                if item is None and key not in schema.get("required", []):
                    continue  # an explicit null for an optional field means "not given"
                item, sub = validate(item, props[key], f"{path + '.' if path else ''}{key}")
                cleaned_obj[key] = item
                problems += sub
            elif schema.get("additionalProperties", True) is False:
                near = _nearest(key, props)
                hint = f" (did you mean {near!r}?)" if near else ""
                problems.append(f"unknown field {key!r}{hint}; allowed: {', '.join(sorted(props)) or 'none'}")
            else:
                cleaned_obj[key] = item
        value = cleaned_obj
    return value, problems


def _coerce(value: Any, type_list: list[str]) -> tuple[Any, bool]:
    for name in type_list:
        expected = _TYPES.get(name)
        if expected is None:
            return value, True
        if name in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, expected):
            if name == "integer" and isinstance(value, float):
                continue
            return value, True
    if isinstance(value, str):
        text = value.strip()
        if "integer" in type_list:
            try:
                return int(text), True
            except ValueError:
                pass
        if "number" in type_list:
            try:
                return float(text), True
            except ValueError:
                pass
        if "boolean" in type_list and text.lower() in ("true", "false", "yes", "no"):
            return text.lower() in ("true", "yes"), True
        if "array" in type_list and text:
            return [text], True
    if isinstance(value, float) and "integer" in type_list and value.is_integer():
        return int(value), True
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "string" in type_list:
        return str(value), True
    return value, False


def _nearest(key: str, props: dict[str, Any]) -> str | None:
    import difflib

    match = difflib.get_close_matches(key, list(props), n=1, cutoff=0.75)
    return match[0] if match else None
