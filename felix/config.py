"""Configuration: defaults, a JSON file on disk, and dotted-path access.

Secrets (passwords, API keys, tokens) never live here - see ``secrets.py``.
"""

from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any

from .util import atomic_write, parse_hhmm

DEFAULTS: dict[str, Any] = {
    "owner": {
        "name": "",
        "email": "",
        "phone": "",          # E.164, e.g. +919812345678 - used for WhatsApp self-messages
        "timezone": "",       # IANA name; empty = system zone
        "date_order": "dmy",  # how to read 03/04: "dmy" or "mdy"
        "vips": [],           # names / emails / numbers that always rank first
    },
    # auto: detect connectivity. offline: never touch the network. online: assume connected.
    "mode": "auto",
    "brain": {
        "model": "claude-opus-5-5",
        "effort": "low",          # low keeps replies quick; raise for harder questions
        "fast": False,            # premium-priced fast output mode
        "max_tokens": 16000,
        "max_iterations": 12,     # tool round-trips per request
        "web_search": True,
        "threads": True,          # send only new messages each turn (lower latency)
        "refusal_fallback": True,
        "share_bodies_with_cloud": True,  # False: only sender/subject/snippets leave the laptop
        "local": {"enabled": True, "url": "http://127.0.0.1:11434", "model": "llama3.1", "timeout": 120},
    },
    "server": {"host": "127.0.0.1", "port": 8765},
    "phone": {
        "enabled": False,
        "bind": "0.0.0.0",
        "port": 8766,
        "tls": True,
        "advertise_hosts": [],    # extra hostnames/IPs to put in the pairing QR (e.g. a Tailscale name)
        "notification_apps": [
            "com.whatsapp", "com.whatsapp.w4b", "com.google.android.apps.messaging",
            "com.samsung.android.messaging", "com.microsoft.teams", "com.Slack",
            "org.telegram.messenger", "org.thoughtcrime.securesms",
        ],
    },
    "mail": {"accounts": [], "sync_minutes": 3, "sync_days": 14, "max_body_chars": 20000, "quote_replies": True},
    "calendar": {
        "sources": [{"id": "local", "kind": "local", "name": "Felix"}],
        "default_source": "local",
        "sync_minutes": 10,
        "work_hours": {"start": "09:00", "end": "18:00", "days": [0, 1, 2, 3, 4]},
        "default_minutes": 30,
    },
    "whatsapp": {
        "transports": ["bridge", "phone", "cloud"],   # tried in this order
        "bridge": {"enabled": False, "port": 8770, "command_chat": True},
        "cloud": {"enabled": False, "phone_number_id": "", "api_version": "v21.0", "template": "", "template_lang": "en"},
    },
    "briefs": {
        "morning": {"enabled": True, "time": "07:30"},
        "evening": {"enabled": True, "time": "21:00"},
        "channels": ["whatsapp", "phone", "email", "desktop"],   # first that succeeds wins
        "catch_up_hours": 4,     # send late if the laptop was asleep at the scheduled time
        "weekends": True,
        "max_chars": 3500,
    },
    "policy": {
        # ask: wait for your approval. auto: act immediately. never: refuse.
        "autonomy": {
            "email": "ask", "whatsapp": "ask", "sms": "ask", "call": "ask", "calendar_invite": "ask",
            "calendar": "auto", "shell": "ask", "files_write": "ask", "files_delete": "ask",
            "power": "ask", "phone_control": "auto", "laptop_control": "auto", "phone_ui": "ask",
            "files_sensitive": "ask",
        },
        "trusted_recipients": [],   # addresses / numbers that may be messaged without asking
        "approval_ttl_minutes": 720,
    },
    "pulse": {"window_hours": 24, "feeds": [], "watch": [], "feed_minutes": 30},
    "laptop": {"shell_enabled": True, "shell_timeout": 60, "screenshot_dir": ""},
    "weather": {"enabled": False, "city": "", "latitude": None, "longitude": None},
    "ui": {"theme": "auto", "open_browser": True},
}

_AUTONOMY = {"ask", "auto", "never"}
# Channels where an unattended mistake cannot be taken back; "auto" is refused
# unless the owner also sets policy.allow_unattended_danger.
DANGER_CHANNELS = {"shell", "files_delete", "power", "files_sensitive"}


class ConfigError(ValueError):
    pass


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """Thread-safe view over the merged defaults + user file."""

    def __init__(self, path: Path | None = None, data: dict[str, Any] | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._user: dict[str, Any] = {}
        if data is not None:
            self._user = copy.deepcopy(data)
        elif path is not None and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ConfigError(f"{path} must contain a JSON object")
            self._user = loaded
        self._merged = _merge(DEFAULTS, self._user)

    # -- reading ---------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            node: Any = self._merged
            for part in dotted.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return copy.deepcopy(node)

    def all(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._merged)

    def user_values(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._user)

    # -- writing ---------------------------------------------------------
    def set(self, dotted: str, value: Any, save: bool = True) -> None:
        validate(dotted, value)
        with self._lock:
            node = self._user
            parts = dotted.split(".")
            for part in parts[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node[parts[-1]] = copy.deepcopy(value)
            self._merged = _merge(DEFAULTS, self._user)
            if save:
                self.save()

    def update(self, values: dict[str, Any], save: bool = True) -> None:
        """Deep-merge a nested dict of changes (used by the settings screen)."""
        for dotted, value in _flatten(values):
            validate(dotted, value)
        with self._lock:
            self._user = _merge(self._user, values)
            self._merged = _merge(DEFAULTS, self._user)
            if save:
                self.save()

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            atomic_write(self.path, json.dumps(self._user, indent=2, ensure_ascii=False) + "\n", private=True)


def _flatten(values: dict[str, Any], prefix: str = ""):
    for key, value in values.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _flatten(value, dotted + ".")
        else:
            yield dotted, value


def validate(dotted: str, value: Any) -> None:
    """Reject values that would silently break Felix later."""
    if dotted == "mode" and value not in ("auto", "online", "offline"):
        raise ConfigError("mode must be auto, online or offline")
    if dotted.startswith("policy.autonomy."):
        if value not in _AUTONOMY:
            raise ConfigError(f"{dotted} must be one of ask, auto, never")
    if dotted in ("briefs.morning.time", "briefs.evening.time"):
        if not isinstance(value, str) or parse_hhmm(value, (-1, -1)) == (-1, -1):
            raise ConfigError(f"{dotted} must be a time like 07:30")
    if dotted == "brain.effort" and value not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("brain.effort must be low, medium, high, xhigh or max")
    if dotted in ("server.port", "phone.port", "whatsapp.bridge.port"):
        if not isinstance(value, int) or not 1 <= value <= 65535:
            raise ConfigError(f"{dotted} must be a port number")
    if dotted == "owner.date_order" and value not in ("dmy", "mdy"):
        raise ConfigError("owner.date_order must be dmy or mdy")
    if dotted == "owner.timezone" and value:
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(str(value))
        except Exception as exc:
            raise ConfigError(f"unknown time zone {value!r}") from exc
