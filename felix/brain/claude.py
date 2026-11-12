"""The online brain: Claude through the Anthropic SDK's tool runner.

Felix's tools are handed to Claude as-is; every call still goes through the
registry, so validation, the approval policy and the audit trail apply. Text
is streamed to whoever is listening as it is produced.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from ..tools import ToolContext
from .prompts import SYSTEM

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix

log = logging.getLogger("felix.brain.claude")

Emit = Callable[[dict[str, Any]], None]

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
FALLBACK_MODELS = ("claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-sonnet-5-5")
# Models that run in fast mode with a plain `speed: "fast"`.
FAST_MODELS = ("claude-opus-5-5", "claude-fable-5-2", "claude-mythos-5-2")
WEB_SEARCH_MODELS = ("claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
                     "claude-opus-4-6", "claude-sonnet-5", "claude-sonnet-4-6")
MAX_TOOL_RESULT_CHARS = 60_000
JSON_RETRIES = 2


class EngineError(Exception):
    """The engine could not answer. ``reason`` is safe to show the owner."""

    def __init__(self, reason: str, kind: str = "unavailable") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind        # auth | config | busy | network | history | unavailable


def sdk_installed() -> bool:
    """Whether the `anthropic` package is there, without importing it."""
    try:
        return importlib.util.find_spec("anthropic") is not None
    except (ImportError, ValueError):
        return False


class ClaudeEngine:
    name = "claude"

    def __init__(self, app: "Felix") -> None:
        self.app = app
        self._client: Any = None
        self._client_key: tuple[Any, ...] | None = None

    # -- availability ----------------------------------------------------------
    def available(self) -> bool:
        """True when the SDK is installed and some credential exists.

        This is asked on every status request, so it only looks for the SDK. Loading
        it takes about a second and is left to ``Brain.warm_up`` or the first question.
        """
        if not sdk_installed():
            return False
        if self.app.secrets.get("anthropic_api_key") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return True
        if os.environ.get("FELIX_IGNORE_ANTHROPIC_PROFILE"):
            return False
        return (Path.home() / ".config" / "anthropic").is_dir()      # an `ant auth login` profile

    def model(self) -> str:
        return str(self.app.config.get("brain.model", "claude-opus-5-5"))

    def client(self) -> Any:
        import anthropic

        key = self.app.secrets.get("anthropic_api_key")
        base_url = self.app.config.get("brain.base_url") or None
        signature = (key, base_url, self.app.config.get("brain.max_retries", 2))
        if self._client is None or self._client_key != signature:
            options: dict[str, Any] = {"max_retries": int(self.app.config.get("brain.max_retries", 2)),
                                       "timeout": anthropic.Timeout(300.0, connect=10.0)}
            if key:
                options["api_key"] = key
            if base_url:
                options["base_url"] = base_url
            self._client = anthropic.Anthropic(**options)
            self._client_key = signature
        return self._client

    # -- request shape -----------------------------------------------------------
    def _request_options(self) -> dict[str, Any]:
        config = self.app.config.get("brain", {})
        model = self.model()
        options: dict[str, Any] = {
            "model": model,
            "max_tokens": int(config.get("max_tokens", 64000)),
            "output_config": {"effort": config.get("effort", "low")},
        }
        if config.get("refusal_fallback", True) and model.startswith(FALLBACK_MODELS):
            # If a safety classifier declines a request, the API retries it on the
            # model Anthropic recommends for that case instead of returning nothing.
            options["betas"] = [FALLBACK_BETA]
            options["fallbacks"] = "default"
        if config.get("fast") and model.startswith(FAST_MODELS):
            options["speed"] = "fast"
        return options

    def _tools(self, ctx: ToolContext, emit: Emit) -> list[Any]:
        from anthropic import beta_tool
        from anthropic.lib.tools import ToolError as SdkToolError

        # Tool inputs stream as they are generated only when talking to the real
        # API; a gateway in between may reject the field.
        eager = None if self.app.config.get("brain.base_url") else True
        registry = self.app.tools
        tools: list[Any] = []
        for tool in registry.all():
            def run(_name: str = tool.name, **arguments: Any) -> str:
                emit({"type": "tool", "name": _name, "status": "start"})
                result = registry.execute(_name, arguments, ctx)
                emit({"type": "tool", "name": _name, "status": "end", "ok": result.ok,
                      "pending": bool(result.pending)})
                if result.pending:
                    emit({"type": "approval", "id": result.pending["id"], "tool": _name,
                          "preview": result.pending.get("preview"), "risk": result.pending.get("risk")})
                if not result.ok:
                    raise SdkToolError(result.error or "the tool failed")
                text = result.for_model()
                if len(text) > MAX_TOOL_RESULT_CHARS:
                    text = text[:MAX_TOOL_RESULT_CHARS] + "\n[result cut: ask for a narrower query]"
                return text

            tools.append(beta_tool(run, name=tool.name, description=tool.description, input_schema=tool.schema,
                                   eager_input_streaming=eager))
        if self.app.config.get("brain.web_search", True) and self.model().startswith(WEB_SEARCH_MODELS):
            tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 5})
        return tools

    # -- one turn ------------------------------------------------------------------
    def run(self, history: list[dict[str, Any]], ctx: ToolContext, emit: Emit) -> dict[str, Any]:
        """Answer the last user message in ``history``.

        Returns {"text", "messages" (to append to the history), "input_tokens",
        "stop", "model"}. Raises EngineError when Claude cannot be used.
        """
        try:
            import anthropic
        except ImportError as exc:        # present on disk but it does not load
            raise EngineError("The Claude SDK could not be loaded. Reinstall it with: pip install -U anthropic",
                              "config") from exc

        options = self._request_options()
        if not self.app.config.get("brain.threads", True):
            options["thread"] = False
        produced: list[dict[str, Any]] = []
        texts: list[str] = []
        usage = {"input": 0, "model": options["model"]}
        stop = "end_turn"
        failures = 0
        while True:
            try:
                runner = self.client().beta.messages.tool_runner(
                    system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                    tools=self._tools(ctx, emit),
                    messages=history + produced,
                    stream=True,
                    cache_control={"type": "ephemeral"},
                    max_iterations=int(self.app.config.get("brain.max_iterations", 12)),
                    **options,
                )
                for stream in runner:
                    wrote = False
                    for event in stream:
                        if event.type == "text":
                            if not wrote and texts:
                                emit({"type": "delta", "text": "\n\n"})
                            wrote = True
                            emit({"type": "delta", "text": event.text})
                    message = stream.get_final_message()
                    produced.append({"role": "assistant", "content": _echo(message.content)})
                    usage["model"] = message.model
                    usage["input"] = _input_tokens(message.usage)
                    text = "".join(block.text for block in message.content if block.type == "text").strip()
                    if text:
                        texts.append(text)
                    stop = message.stop_reason or "end_turn"
                    if stop == "refusal":
                        break
                    if stop == "max_tokens" and any(block.type == "tool_use" for block in message.content):
                        break       # a cut-off tool call must never run
                    results = runner.generate_tool_call_response()
                    if results is not None:
                        produced.append(dict(results))
                break
            except ValueError as exc:
                # The SDK could not parse a streamed tool input at all. Nothing ran for
                # that turn; ask again from what we have, a bounded number of times.
                failures += 1
                if failures > JSON_RETRIES:
                    raise EngineError("Claude kept sending a malformed tool call.", "busy") from exc
                continue
            except anthropic.AuthenticationError as exc:
                raise EngineError("Claude rejected the API key. Set a valid one with: felix connect claude", "auth") from exc
            except anthropic.PermissionDeniedError as exc:
                raise EngineError(f"This API key may not use {options['model']}.", "auth") from exc
            except anthropic.NotFoundError as exc:
                raise EngineError(f"Claude does not know the model {options['model']!r}; check brain.model.", "config") from exc
            except anthropic.RateLimitError as exc:
                raise EngineError("Claude is rate-limiting this account right now.", "busy") from exc
            except anthropic.BadRequestError as exc:
                raise EngineError(f"Claude refused the request: {_message(exc)}", "history") from exc
            except anthropic.APIStatusError as exc:
                raise EngineError(f"Claude is having trouble ({exc.status_code}).", "busy") from exc
            except anthropic.APIConnectionError as exc:
                raise EngineError("Cannot reach Claude.", "network") from exc

        if stop == "refusal":
            # Whatever was produced in a declined turn is discarded, not replayed.
            return {"text": "I can't help with that request.", "messages": [], "input_tokens": usage["input"],
                    "stop": "refusal", "model": usage["model"]}
        text = "\n\n".join(texts)
        if stop == "max_tokens":
            text = (text + "\n\n" if text else "") + "(I ran out of room before finishing - ask me to continue.)"
        return {"text": text or "(no reply)", "messages": produced, "input_tokens": usage["input"], "stop": stop,
                "model": usage["model"]}

    def complete(self, system: str, prompt: str, max_tokens: int = 16000) -> str:
        """One plain request with no tools (used to polish briefs)."""
        import anthropic

        options = self._request_options()
        options["max_tokens"] = max_tokens
        try:
            with self.client().beta.messages.stream(
                system=system, messages=[{"role": "user", "content": prompt}], **options,
            ) as stream:
                message = stream.get_final_message()
        except anthropic.APIError as exc:
            raise EngineError(f"Claude could not be reached: {_message(exc)}", "network") from exc
        if message.stop_reason == "refusal":
            raise EngineError("Claude declined the request.", "busy")
        return "".join(block.text for block in message.content if block.type == "text").strip()


def _echo(content: list[Any]) -> list[dict[str, Any]]:
    """Turn a reply's content blocks into what must be sent back next time.

    Blocks go back exactly as the API returned them (thinking blocks included).
    The one exception: after a refusal fallback, model-internal blocks that
    precede the final ``fallback`` block belong to the model that declined and
    are left out, while the fallback marker itself stays in place.
    """
    blocks = [block.to_dict(mode="json") if hasattr(block, "to_dict") else dict(block) for block in content]
    boundary = max((i for i, block in enumerate(blocks) if block.get("type") == "fallback"), default=-1)
    if boundary < 0:
        return blocks
    kept = [block for block in blocks[:boundary] if block.get("type") in ("text", "fallback")]
    return kept + blocks[boundary:]


def _input_tokens(usage: Any) -> int:
    total = 0
    for field in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        total += int(getattr(usage, field, 0) or 0)
    return total


def _message(exc: Exception) -> str:
    text = getattr(exc, "message", None) or str(exc)
    return str(text)[:300]
