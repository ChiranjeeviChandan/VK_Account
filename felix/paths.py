"""Where Felix keeps its data on each operating system."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_home() -> Path:
    """Return the directory that holds Felix's database, config and secrets.

    ``FELIX_HOME`` overrides the platform default.
    """
    override = os.environ.get("FELIX_HOME")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Felix"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Felix"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "felix"


class Paths:
    """Resolved file locations under one Felix home directory."""

    def __init__(self, home: str | os.PathLike[str] | None = None) -> None:
        self.home = Path(home).expanduser() if home else default_home()

    def ensure(self) -> "Paths":
        self.home.mkdir(parents=True, exist_ok=True)
        restrict(self.home, directory=True)
        for sub in (self.logs, self.tls, self.files):
            sub.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def config(self) -> Path:
        return self.home / "config.json"

    @property
    def secrets(self) -> Path:
        return self.home / "secrets.json"

    @property
    def db(self) -> Path:
        return self.home / "felix.db"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def tls(self) -> Path:
        return self.home / "tls"

    @property
    def files(self) -> Path:
        """Screenshots, saved attachments and other files Felix produces."""
        return self.home / "files"

    @property
    def bridge_session(self) -> Path:
        return self.home / "whatsapp-session"


def restrict(path: Path, directory: bool = False) -> None:
    """Make ``path`` readable by the current user only (no-op on Windows,
    where the per-user profile ACL already applies)."""
    if sys.platform == "win32":
        return
    try:
        os.chmod(path, 0o700 if directory else 0o600)
    except OSError:
        pass
