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
        self._background = False
        self._lock = threading.RLock()
        self.stopping = threading.Event()      # set to ask whoever started Felix to shut it down
        self._build_services()

    # -- services --------------------------------------------------------------
    def _build_services(self) -> None:
        from .brain.core import Brain
        from .briefing import Briefing
        from .connectors.calendar import Calendar
        from .connectors.feeds import Feeds
        from .connectors.mail import Mail
        from .connectors.oauth import OAuth
        from .contacts import Contacts
        from .control.laptop import Laptop
        from .insights.pulse import Pulse
        from .memory import Memory
        from .outbox import Outbox
        from .phone.hub import PhoneHub
        from .scheduler import Scheduler
        from .server.api import Server
        from .tasks import Tasks
        from .whatsapp.service import WhatsApp

        self.contacts = Contacts(self)
        self.tasks = Tasks(self)
        self.memory = Memory(self)
        self.pulse = Pulse(self)
        self.outbox = Outbox(self)
        self.oauth = OAuth(self)
        self.mail = Mail(self)
        self.calendar = Calendar(self)
        self.whatsapp = WhatsApp(self)
        self.phone = PhoneHub(self)
        self.laptop = Laptop(self)
        self.feeds = Feeds(self)
        self.briefing = Briefing(self)
        self.scheduler = Scheduler(self)
        self.brain = Brain(self)
        self.server = Server(self)
        for service in (self.contacts, self.tasks, self.memory, self.pulse, self.mail, self.calendar,
                        self.whatsapp, self.phone, self.laptop, self.briefing):
            service.register(self.tools)

    # -- lifecycle -------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        """Switch between auto / online / offline and remember the choice."""
        self.net.set_mode(mode)
        self.config.set("mode", mode)
        self.store.audit("owner", "mode", {"mode": mode})
        self.bus.publish("mode.changed", self.net.describe())
        # Offline means no connections at all, so the WhatsApp link goes down with it.
        bridge = self.whatsapp.bridge
        if mode == "offline":
            if bridge.state != "stopped":
                bridge.stop()
        elif self._started and self._background and bridge.enabled() and bridge.state == "stopped":
            bridge.start()

    def owner_name(self) -> str:
        return (self.config.get("owner.name") or "").strip()

    @property
    def sandbox(self) -> bool:
        """The demo: sample data, and nothing may leave this laptop."""
        return bool(self.config.get("sandbox", False))

    def sent_note(self) -> str:
        """Appended to 'Sent' confirmations so the demo never claims a real delivery."""
        return " (Demo: recorded here, not really sent.)" if self.sandbox else ""

    def start(self, port: int | None = None, background_jobs: bool = True) -> None:
        """Bring Felix up: local server, connectivity monitor, scheduler, bridge."""
        with self._lock:
            if self._started:
                return
            self.server.start(port)
            self.net.start()
            self._background = background_jobs
            if background_jobs:
                self.brain.warm_up()
                self.scheduler.start()
                if self.whatsapp.bridge.enabled() and self.net.mode != "offline":
                    self.whatsapp.bridge.start()
            self._started = True
            log.info("Felix %s is up on %s", self.version, self.server.url())

    def close(self) -> None:
        with self._lock:
            started, self._started = self._started, False
        self.scheduler.stop()
        self.net.stop()
        self.whatsapp.bridge.stop() if self.whatsapp.bridge.state != "stopped" else None
        if started or self.server.local is not None:
            self.server.stop()
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
