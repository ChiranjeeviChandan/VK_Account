import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("FELIX_NO_KEYRING", "1")

from felix.app import Felix  # noqa: E402
from felix.tools import ToolContext  # noqa: E402
from felix.util import FixedClock  # noqa: E402

TZ = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 11, 11, 10, 0, tzinfo=TZ)  # Wednesday morning


class Net:
    """Switchable fake connectivity for tests."""

    def __init__(self) -> None:
        self.up = False

    def __call__(self) -> bool:
        return self.up


@pytest.fixture
def net():
    return Net()


@pytest.fixture
def app(tmp_path, net, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    felix = Felix(
        home=tmp_path / "felix-home",
        clock=FixedClock(NOW),
        probe=net,
        config={
            "owner": {"name": "VK", "email": "vk@example.com", "phone": "+919800000001",
                      "timezone": "Asia/Kolkata"},
        },
        use_keyring=False,
    )
    felix.net._ttl = 0  # every check re-reads the fake
    yield felix
    felix.close()


@pytest.fixture
def ctx(app):
    return ToolContext(app, thread="test", source="test", actor="felix")


@pytest.fixture
def owner(app):
    return ToolContext(app, thread="test", source="test", actor="owner")
