"""The Felix application object: owns every service and wires them together."""

from __future__ import annotations

import logging
import logging.handlers
import os
import threading
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .bus import Bus
from .config import Config
from .net import Connectivity
from .paths import Paths
from .policy import Policy
from .secrets import SecretStore
from .store import Store
from .tools import Registry
from .util import Clock, resolve_tz

log = logging.getLogger("felix")


class Felix:
    """Everything Felix can do hangs off one instance of this class.

    Tests build it against a temporary directory with a fixed clock and a fake
    connectivity probe; nothing touches the real network or the user's data.
    """

    def __init__(
        self,
        home: str | os.PathLike[str] | None = None,
        clock: Clock | None = None,
        probe: Callable[[], bool] | None = None,
        config: dict[str, Any] | None = None,
        use_keyring: bool = True,
    ) -> None:
        self.version = __version__
        self.paths = Paths(home).ensure()
        self.config = Config(self.paths.config, data=config) if config is not None else Config(self.paths.config)
        if config is not None:
            self.config.path = self.paths.config
        self.secrets = SecretStore(self.paths.secrets, use_keyring=use_keyring)
        self.clock = clock or Clock(resolve_tz(self.config.get("owner.timezone") or None))
        self.store = Store(self.paths.db)
        self.bus = Bus()
        self.net = Connectivity(
            mode=self.config.get("mode", "auto"),
            probe=probe,
            on_change=lambda state: self.bus.publish("net.changed", {"online": state}),
        )
        self.tools = Registry(self)
        self.policy = Policy(self)
        self._started = False
        self._lock = threading.RLock()
        self._build_services()

    # -- services --------------------------------------------------------------
    def _build_services(self) -> None:
        from .connectors.mail import Mail
        from .connectors.oauth import OAuth
        from .contacts import Contacts
        from .insights.pulse import Pulse
        from .memory import Memory
        from .outbox import Outbox
        from .tasks import Tasks

        self.contacts = Contacts(self)
        self.tasks = Tasks(self)
        self.memory = Memory(self)
        self.pulse = Pulse(self)
        self.outbox = Outbox(self)
        self.oauth = OAuth(self)
        self.mail = Mail(self)
        for service in (self.contacts, self.tasks, self.memory, self.pulse, self.mail):
            service.register(self.tools)

    # -- lifecycle -------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        """Switch between auto / online / offline and remember the choice."""
        self.net.set_mode(mode)
        self.config.set("mode", mode)
        self.store.audit("owner", "mode", {"mode": mode})
        self.bus.publish("mode.changed", self.net.describe())

    def owner_name(self) -> str:
        return (self.config.get("owner.name") or "").strip()

    def close(self) -> None:
        self.net.stop()
        self.store.close()


def configure_logging(paths: Paths, verbose: bool = False) -> None:
    """Rotating log file plus a quiet console. Message bodies are never logged."""
    root = logging.getLogger()
    if any(getattr(h, "_felix", False) for h in root.handlers):
        return
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            Path(paths.logs) / "felix.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler._felix = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)
    except OSError:
        pass
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    console._felix = True  # type: ignore[attr-defined]
    root.addHandler(console)
