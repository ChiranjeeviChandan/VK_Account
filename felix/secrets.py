"""Secret storage: OS keychain when one works, a private file otherwise.

Nothing in here is ever written to the config file, logs or the audit trail.
Environment variables win, so a key can be supplied without touching disk:
``FELIX_SECRET_ANTHROPIC_API_KEY`` or the conventional ``ANTHROPIC_API_KEY``.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from .util import atomic_write

SERVICE = "felix-assistant"
_ENV_ALIASES = {"anthropic_api_key": ("ANTHROPIC_API_KEY",)}


def _env_name(name: str) -> str:
    return "FELIX_SECRET_" + re.sub(r"[^A-Za-z0-9]+", "_", name).upper()


class SecretStore:
    def __init__(self, path: Path | None, use_keyring: bool = True) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._keyring = _working_keyring() if use_keyring else None

    @property
    def backend(self) -> str:
        return "keychain" if self._keyring else "file"

    def get(self, name: str, default: str | None = None) -> str | None:
        for env in (_env_name(name), *_ENV_ALIASES.get(name, ())):
            value = os.environ.get(env)
            if value:
                return value
        with self._lock:
            if self._keyring is not None:
                try:
                    value = self._keyring.get_password(SERVICE, name)
                    if value is not None:
                        return value
                except Exception:
                    pass
            return self._read_file().get(name, default)

    def set(self, name: str, value: str) -> None:
        with self._lock:
            if self._keyring is not None:
                try:
                    self._keyring.set_password(SERVICE, name, value)
                    self._index(name, present=True)
                    return
                except Exception:
                    self._keyring = None  # keychain broke mid-run: fall back to the file
            data = self._read_file()
            data[name] = value
            self._write_file(data)

    def delete(self, name: str) -> None:
        with self._lock:
            if self._keyring is not None:
                try:
                    self._keyring.delete_password(SERVICE, name)
                except Exception:
                    pass
                self._index(name, present=False)
            data = self._read_file()
            if name in data:
                del data[name]
                self._write_file(data)

    def has(self, name: str) -> bool:
        return bool(self.get(name))

    def names(self) -> list[str]:
        """Names only - used by the settings screen to show what is configured."""
        with self._lock:
            data = self._read_file()
            names = set(k for k in data if not k.startswith("__"))
            names.update(data.get("__keychain_index__", []))
            return sorted(names)

    # -- file backend ----------------------------------------------------
    def _read_file(self) -> dict:
        if self.path is None or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_file(self, data: dict) -> None:
        if self.path is None:
            return
        atomic_write(self.path, json.dumps(data, indent=2) + "\n", private=True)

    def _index(self, name: str, present: bool) -> None:
        """Keychains cannot list entries, so remember the names (not values)."""
        data = self._read_file()
        index = set(data.get("__keychain_index__", []))
        if present:
            index.add(name)
        else:
            index.discard(name)
        data["__keychain_index__"] = sorted(index)
        self._write_file(data)


def _working_keyring():
    """Return the keyring module if a real backend is usable, else None."""
    if os.environ.get("FELIX_NO_KEYRING"):
        return None
    try:
        import keyring
        from keyring.backends import fail

        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring):
            return None
        priority = getattr(backend, "priority", 1)
        if priority is not None and priority <= 0:
            return None
        return keyring
    except Exception:
        return None
