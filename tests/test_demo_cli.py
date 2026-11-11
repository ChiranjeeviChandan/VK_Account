"""The demo (sample data in a sandbox), the `felix` command and the local client."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from felix import cli
from felix.app import Felix
from felix.client import CoreClient, CoreUnavailable
from felix.demo import seed
from felix.tools import ToolContext, ToolError

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def demo(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "FELIX_SECRET_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    felix = Felix(home=tmp_path / "demo", probe=lambda: True, use_keyring=False,
                  config={"sandbox": True, "owner": {"name": "Vikram"}})
    counts = seed(felix)
    yield felix, counts
    felix.close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# -- sample data -------------------------------------------------------------------------

def test_seed_builds_a_believable_day(demo):
    app, counts = demo
    assert counts["emails"] == 10 and counts["messages"] == 6 and counts["brief_chars"] > 200
    report = app.pulse.report()
    assert [issue["title"] for issue in report["broken"]][:2] == ["Payment gateway down",
                                                                  "Extremely disappointed with the rollout"]
    assert any(person["who"] == "Anita Rao" and person["vip"] for person in report["people"])
    assert len(app.policy.pending()) == 1 and app.policy.pending()[0]["tool"] == "email_reply"
    assert len(app.calendar.between(app.clock.now().replace(hour=0, minute=0), app.clock.now().replace(hour=23, minute=59))) >= 4
    titles = [task["title"] for task in app.tasks.suggestions()]
    assert "Review the revenue slide before Thursday's call (asked by Priya Nair)" in titles
    assert "Give your go-ahead: backup gateway is tested and ready (asked by Ops Desk)" in titles
    assert app.sandbox and [a.email for a in app.mail.accounts()] == ["you@northwind.example"]
    assert "WHAT IS BROKEN (3)" in app.briefing.render(app.briefing.gather("now"), style="plain")


# -- the sandbox: nothing may leave the laptop ----------------------------------------------

def test_sandbox_records_an_approved_email_without_sending(demo, monkeypatch):
    app, _ = demo

    def forbidden(*args, **kwargs):
        raise AssertionError("the demo tried to open a network connection")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    approval = app.policy.pending()[0]
    original = app.store.one("SELECT * FROM emails WHERE id = ?", (approval["args"]["id"],))
    result = app.policy.approve(approval["id"], via="test")
    assert result.ok and result.data["status"] == "sent"
    assert result.data["message"] == "Sent to meera@northwind.example. (Demo: recorded here, not really sent.)"
    sent = app.mail.list(direction="out")
    assert len(sent) == 1 and sent[0]["subject"] == "Re: Q4 budget" and sent[0]["to_addrs"] == ["meera@northwind.example"]
    assert "Approved - please go ahead" in sent[0]["body"]
    assert app.store.one("SELECT is_answered FROM emails WHERE id = ?", (original["id"],))["is_answered"] == 1
    assert app.outbox.recent(1)[0]["status"] == "sent" and not app.outbox.pending()
    assert app.store.one("SELECT outcome FROM audit WHERE action = 'outbox.email'")["outcome"] == "demo: not sent"
    # a second reply gets its own place in the local Sent folder
    owner = ToolContext(app, thread="t", source="test", actor="owner", preapproved=True)
    assert app.tools.execute("email_reply", {"id": original["id"], "body": "One more thing."}, owner).ok
    assert len(app.mail.list(direction="out")) == 2


def test_sandbox_whatsapp_and_sync_stay_local(demo, monkeypatch):
    app, _ = demo
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")))
    owner = ToolContext(app, thread="t", source="test", actor="owner", preapproved=True)
    result = app.tools.execute("whatsapp_send", {"to": "Ravi Kumar", "text": "Calling you in 10 minutes."}, owner)
    assert result.ok and result.data["message"] == "Sent to Ravi Kumar on WhatsApp. (Demo: recorded here, not really sent.)"
    chat = app.whatsapp.find_chat("Ravi Kumar")
    history = app.whatsapp.history(chat["id"])
    assert history[-1]["direction"] == "out" and history[-1]["text"] == "Calling you in 10 minutes."
    assert not app.whatsapp.public_chat(chat).get("needs_reply")
    assert app.mail.sync() == {"new": 0, "updated": 0, "removed": 0, "errors": []}


def test_sandbox_cannot_pair_a_phone_or_link_whatsapp(demo):
    app, _ = demo
    with pytest.raises(ToolError, match="cannot pair a phone"):
        app.phone.new_pairing()
    app.whatsapp.bridge.start()
    assert app.whatsapp.bridge.state == "error" and "demo" in app.whatsapp.bridge.last_error.lower()
    assert not app.whatsapp.bridge.ready()


def test_outside_the_sandbox_the_real_handler_runs(app):
    calls = []
    app.outbox.handler("probe", calls.append, rehearse=lambda payload: calls.append("rehearsed"))
    assert not app.sandbox and app.sent_note() == ""
    app.outbox.enqueue("probe", {"n": 1}, "probe")
    assert calls == [{"n": 1}]
    app.config.set("sandbox", True)
    app.outbox.enqueue("probe", {"n": 2}, "probe")
    assert calls == [{"n": 1}, "rehearsed"]


# -- the command line -----------------------------------------------------------------------

def test_autostart_files_for_each_platform(tmp_path):
    path, body, command = cli.autostart_file("enable", "linux", tmp_path, "/usr/bin/python3")
    assert path == tmp_path / ".config/systemd/user/felix.service"
    assert "ExecStart=/usr/bin/python3 -m felix start --no-browser" in body and "systemctl --user" in command
    path, body, command = cli.autostart_file("enable", "darwin", tmp_path, "/usr/bin/python3")
    assert path.name == "ai.felix.core.plist" and "<string>/usr/bin/python3</string>" in body and "launchctl load" in command
    path, body, _ = cli.autostart_file("enable", "win32", tmp_path, r"C:\Py\python.exe")
    assert path.name == "Felix.cmd" and r'"C:\Py\pythonw.exe" -m felix start --no-browser' in body
    assert cli.autostart_file("disable", "linux", tmp_path)[1] is None


def test_commands_that_need_a_running_core_say_so(tmp_path, capsys):
    home = str(tmp_path / "nothing-here")
    port = free_port()
    Felix(home=home, config={"server": {"port": port}}, use_keyring=False).close()   # writes nothing: just a folder
    client = CoreClient(home, port=port)
    assert not client.running()
    with pytest.raises(CoreUnavailable, match="felix start"):
        client.get("/api/status")
    assert cli.main(["--home", home, "status"]) == 2
    assert "Felix is not running" in capsys.readouterr().out


def test_ask_answers_in_process_when_nothing_is_running(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FELIX_NO_KEYRING", "1")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "FELIX_SECRET_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    home = str(tmp_path / "solo")
    assert cli.main(["--home", home, "config", "set", "server.port", str(free_port())]) == 0
    assert cli.main(["--home", home, "config", "set", "mode", "offline"]) == 0
    capsys.readouterr()
    assert cli.main(["--home", home, "ask", "add", "task", "Call", "the", "bank", "tomorrow", "5pm"]) == 0
    assert "Added to your list: #1 Call the bank" in capsys.readouterr().out
    assert cli.main(["--home", home, "ask", "what", "is", "on", "my", "list"]) == 0
    assert "Call the bank" in capsys.readouterr().out
    assert cli.main(["--home", home, "config", "get", "mode"]) == 0
    assert capsys.readouterr().out.strip() == "offline"
    assert cli.main(["--home", home, "config", "set", "mode", "sideways"]) == 1


def test_demo_runs_end_to_end_from_the_command_line(tmp_path):
    """Start `felix demo` as a real process and drive it with the CLI and the client."""
    home, port = str(tmp_path / "demo-home"), free_port()
    env = {**os.environ, "FELIX_NO_KEYRING": "1", "FELIX_IGNORE_ANTHROPIC_PROFILE": "1", "PYTHONPATH": str(REPO)}
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "FELIX_SECRET_ANTHROPIC_API_KEY"):
        env.pop(name, None)

    def felix(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "felix", "--home", home, *args], env=env, capture_output=True,
                              text=True, timeout=60)

    server = subprocess.Popen([sys.executable, "-m", "felix", "--home", home, "demo", "--no-browser", "--port", str(port)],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        client = None
        for _ in range(100):
            if server.poll() is not None:
                pytest.fail(f"felix demo exited early:\n{server.stdout.read()}")
            if (Path(home) / "secrets.json").exists():
                client = CoreClient(home, port=port)
                if client.running():
                    break
            time.sleep(0.2)
        else:
            pytest.fail("felix demo did not come up")

        status = client.get("/api/status")
        assert status["sandbox"] is True and status["owner"] == "Vikram" and status["counts"]["approvals"] == 1
        assert client.chat("what's broken")["reply"].startswith("3 things look broken:")

        out = felix("status").stdout
        assert "Felix 0.1.0 is running for Vikram." in out and "1 approval, 0 queued messages" in out
        assert "Reply to email" in felix("pending").stdout
        approved = felix("approve", "1")
        assert approved.returncode == 0 and "Demo: recorded here, not really sent." in approved.stdout
        assert "Nothing is waiting" in felix("pending").stdout
        asked = felix("ask", "who is waiting on me")
        assert asked.returncode == 0 and "Anita Rao" in asked.stdout
        paired = felix("pair")
        assert paired.returncode == 1 and "cannot pair a phone" in paired.stderr
        assert "REST OF TODAY" in felix("brief").stdout.upper() or "TOMORROW" in felix("brief").stdout.upper()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
