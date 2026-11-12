"""The demo (sample data in a sandbox), the `felix` command and the local client."""

import os
import socket
import subprocess
import sys
import threading
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


# -- the desktop overlay, driven without a screen ---------------------------------------------

class FakeWidget:
    """Just enough of a Tk widget to run the overlay's logic and see what it would show."""

    def __init__(self, parent=None, **options):
        self.options, self.children, self.packed, self.bindings, self.value = options, [], False, {}, ""
        self.parent = parent
        if parent is not None:
            parent.children.append(self)

    def pack(self, **kw): self.packed = True
    def pack_forget(self): self.packed = False
    def configure(self, **kw): self.options.update(kw)
    def bind(self, sequence, handler): self.bindings[sequence] = handler
    def winfo_ismapped(self): return self.packed
    def winfo_children(self): return list(self.children)
    def focus_set(self): pass
    def see(self, index): pass

    def destroy(self):
        if self.parent is not None and self in self.parent.children:
            self.parent.children.remove(self)

    def get(self): return self.value
    def insert(self, index, text): self.value += text
    def delete(self, first, last=None): self.value = ""


class FakeRoot(FakeWidget):
    def __init__(self):
        super().__init__()
        self.timers, self.geometry_calls = [], []

    def title(self, text): pass
    def attributes(self, *args): pass
    def winfo_screenwidth(self): return 1920
    def geometry(self, spec): self.geometry_calls.append(spec)
    def resizable(self, *args): pass
    def protocol(self, name, handler): pass
    def after(self, ms, handler): self.timers.append(handler)
    def iconify(self): pass
    def mainloop(self): pass


class FakeTk:
    TclError = RuntimeError
    Tk = FakeRoot
    Entry = Label = Text = Frame = Button = FakeWidget


def test_desktop_overlay_sends_shows_and_decides(app):
    from felix.overlay import Overlay

    app.contacts.upsert("Meera Iyer", phones=["+919800011122"])
    app.start(port=0, background_jobs=False)
    client = CoreClient(str(app.paths.home), port=app.server.local.port, token=app.server.token())
    overlay = Overlay(FakeTk, client)

    def ask(text):
        overlay.entry.value = text
        overlay.send()
        for _ in range(400):                       # the answer arrives on a worker thread
            overlay.pump()
            if not overlay.busy:
                return overlay.output.value
            time.sleep(0.01)
        raise AssertionError("the overlay never got an answer")

    shown = ask("add task call the bank tomorrow")
    assert shown.startswith("You: add task call the bank tomorrow\n\nAdded to your list: #1 Call the bank")
    assert overlay.status.options["text"] == "Offline commands" and overlay.entry.value == ""
    assert overlay.root.geometry_calls[-1] == "620x340" and not overlay.actions.packed

    shown = ask("whatsapp Meera saying Landing at 9")
    assert "Send WhatsApp\nTo: Meera Iyer" in shown
    buttons = [child.options["text"] for child in overlay.actions.children]
    assert buttons == ["Approve #1", "Cancel #1"] and overlay.actions.packed
    overlay.actions.children[1].options["command"]()            # press Cancel
    assert overlay.output.value.endswith("\nCancelled #1.\n") and overlay.actions.children == []
    assert app.policy.get(1)["status"] == "denied"
    overlay.entry.value = "   "
    overlay.send()                                                # nothing typed: nothing happens
    assert not overlay.busy


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


def test_doctor_reports_what_is_missing_without_failing_on_optional_parts(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FELIX_NO_KEYRING", "1")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "FELIX_SECRET_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    home = str(tmp_path / "check")
    assert cli.main(["--home", home, "config", "set", "mode", "offline"]) == 0
    capsys.readouterr()
    assert cli.main(["--home", home, "doctor"]) == 0
    out = capsys.readouterr().out
    assert "[ok  ] Python 3.10 or newer" in out and "[--  ] Claude API key - felix connect claude" in out
    assert "[--  ] WhatsApp bridge installed - optional: felix connect whatsapp" in out
    assert "Everything required is in place." in out
    # the demo folder is recognised: its sample mail account is not a broken sign-in
    demo_home = tmp_path / "demo"
    app = Felix(home=demo_home, probe=lambda: True, use_keyring=False, config={"sandbox": True})
    seed(app)
    app.config.save()
    app.close()
    assert cli.main(["--home", str(demo_home), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "This is the demo folder" in out and "sign-in" not in out


def test_a_closed_pipe_is_not_a_crash(monkeypatch):
    def reader_went_away(args):
        raise BrokenPipeError

    monkeypatch.setitem(cli.HANDLERS, "status", reader_went_away)
    assert cli.main(["status"]) == 0


class Stream:
    """Stands in for the terminal, or for a pipe."""

    def __init__(self, terminal: bool) -> None:
        self.terminal = terminal

    def isatty(self) -> bool:
        return self.terminal

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


def test_a_question_is_only_asked_when_someone_can_answer(monkeypatch):
    monkeypatch.setattr(cli, "_is_console", lambda stream: True)        # a real console window, on Windows
    for keyboard, screen, expected in ((True, True, True), (True, False, False), (False, True, False)):
        monkeypatch.setattr(sys, "stdin", Stream(keyboard))
        monkeypatch.setattr(sys, "stdout", Stream(screen))
        assert cli._at_a_terminal() is expected
    monkeypatch.setattr(sys, "stdin", None)                             # started without a console at all
    monkeypatch.setattr(sys, "stdout", Stream(True))
    assert cli._at_a_terminal() is False


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows calls its empty device a terminal")
def test_the_empty_device_is_not_taken_for_a_console():
    with open(os.devnull) as nothing:
        assert not cli._is_console(nothing)
    assert not cli._is_console(None)


def test_approve_shows_the_action_and_asks_first_at_a_terminal(demo, monkeypatch, capsys):
    app, _ = demo
    app.start(port=0, background_jobs=False)
    home, token = str(app.paths.home), app.server.token()
    monkeypatch.setattr(cli, "_client", lambda args: CoreClient(home, port=app.server.local.port, token=token))
    answers: list[str] = []

    def typed(prompt=""):
        print(prompt, end="")
        return answers.pop(0)

    monkeypatch.setattr("builtins.input", typed)
    monkeypatch.setattr(cli, "_at_a_terminal", lambda: True)

    answers.append("")                                                  # Enter alone means no
    assert cli.main(["--home", home, "approve", "1"]) == 0
    shown = capsys.readouterr().out
    assert "meera@northwind.example" in shown and "Approved - please go ahead" in shown
    assert shown.index("Approved - please go ahead") < shown.index("Go ahead? (y/N)") and shown.rstrip().endswith("Left waiting.")
    assert [a["id"] for a in app.policy.pending()] == [1] and not app.mail.list(direction="out")

    answers.append("y")
    assert cli.main(["--home", home, "approve", "1"]) == 0
    assert "Demo: recorded here, not really sent." in capsys.readouterr().out
    assert not app.policy.pending() and len(app.mail.list(direction="out")) == 1

    # --yes skips the question, and so does a script: there is nobody to ask
    def nobody(prompt=""):
        raise AssertionError("asked a question nobody can answer")

    monkeypatch.setattr("builtins.input", nobody)
    original = app.store.one("SELECT id FROM emails WHERE direction = 'in' ORDER BY id LIMIT 1")["id"]
    for at_a_terminal, extra in ((True, ["--yes"]), (False, [])):
        monkeypatch.setattr(cli, "_at_a_terminal", lambda answer=at_a_terminal: answer)
        asked = app.tools.execute("email_reply", {"id": original, "body": "Thank you."},
                                  ToolContext(app, thread="t", source="test", actor="felix"))
        number = asked.pending["id"]
        assert cli.main(["--home", home, "approve", str(number), *extra]) == 0
        out = capsys.readouterr().out
        assert "Thank you." in out and "Go ahead?" not in out and "Demo: recorded here, not really sent." in out
    assert not app.policy.pending()
    assert cli.main(["--home", home, "approve", "99"]) == 1
    assert "Nothing with the number 99 is waiting" in capsys.readouterr().err


# -- starting and stopping -------------------------------------------------------------------

def test_starting_twice_opens_the_dashboard_instead_of_failing(app, monkeypatch, capsys):
    """A shortcut runs `felix start` every time it is clicked; the second click must not be an error."""
    app.start(port=0, background_jobs=False)
    home, port, token = str(app.paths.home), app.server.local.port, app.server.token()
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda link: opened.append(link) or True)
    assert cli.main(["--home", home, "start", "--port", str(port)]) == 0
    assert "Felix is already running." in capsys.readouterr().out
    assert opened == [f"http://127.0.0.1:{port}/login?token={token}"]
    assert cli.main(["--home", home, "start", "--port", str(port), "--no-browser"]) == 0 and len(opened) == 1
    assert app.server.local is not None, "the Felix that was already running must be left alone"


def test_a_port_taken_by_something_else_is_explained_and_the_window_waits(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FELIX_NO_KEYRING", "1")
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")
    with socket.socket() as other:                        # some other program on the port
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        port = str(other.getsockname()[1])
        home = str(tmp_path / "blocked")
        assert cli.main(["--home", home, "start", "--port", port, "--no-browser"]) == 1
        assert "Another program may be using its port" in capsys.readouterr().err and asked == []
        # from a shortcut the window would vanish with the message, so it waits for Enter
        assert cli.main(["--home", home, "start", "--port", port, "--no-browser", "--keep-open"]) == 1
        assert asked == ["\nPress Enter to close this window."]
    # nothing to wait for when all went well
    assert cli.main(["--home", home, "config", "set", "mode", "offline"]) == 0 and len(asked) == 1


def test_stop_ends_a_running_felix(app, monkeypatch, capsys):
    app.start(port=0, background_jobs=False)
    home, token, port = str(app.paths.home), app.server.token(), app.server.local.port
    monkeypatch.setattr(cli, "_client", lambda args: CoreClient(home, port=port, token=token))

    def run_until_told_to_stop():                         # what `felix start` does after starting
        cli._wait_for_exit(app)
        app.close()

    runner = threading.Thread(target=run_until_told_to_stop, daemon=True)
    runner.start()
    assert cli.main(["--home", home, "stop"]) == 0
    assert "Felix has stopped." in capsys.readouterr().out
    runner.join(10)
    assert not runner.is_alive() and app.server.local is None
    assert cli.main(["--home", home, "stop"]) == 0
    assert "Felix is not running." in capsys.readouterr().out


def test_felix_says_when_it_has_administrator_rights(app, monkeypatch, capsys):
    from felix import util

    with monkeypatch.context() as elsewhere:              # how it is told on macOS and Linux
        elsewhere.setattr(util.sys, "platform", "linux")
        for uid, expected in ((0, True), (1000, False)):
            elsewhere.setattr(util.os, "geteuid", lambda uid=uid: uid, raising=False)
            assert util.elevated() is expected
    assert util.elevated() in (True, False)               # and on this system it answers without failing
    # the terminal says so when Felix starts...
    assert "administrator rights" in cli.ELEVATED_WARNING
    # ...the dashboard is told...
    app.start(port=0, background_jobs=False)
    client = CoreClient(str(app.paths.home), port=app.server.local.port, token=app.server.token())
    for value in (True, False):
        monkeypatch.setattr("felix.server.api.elevated", lambda value=value: value)
        assert client.get("/api/status")["elevated"] is value
    # ...and so is `felix doctor` (which closes the Felix it looked at)
    monkeypatch.setattr(cli, "elevated", lambda: True)
    monkeypatch.setattr(cli, "_app", lambda args: app)
    cli.main(["doctor"])
    assert "[--  ] Running without administrator rights - Felix does not need them" in capsys.readouterr().out


def test_demo_runs_end_to_end_from_the_command_line(tmp_path):
    """Start `felix demo` as a real process and drive it with the CLI and the client."""
    home, port = str(tmp_path / "demo-home"), free_port()
    env = {**os.environ, "FELIX_NO_KEYRING": "1", "FELIX_IGNORE_ANTHROPIC_PROFILE": "1", "PYTHONPATH": str(REPO)}
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "FELIX_SECRET_ANTHROPIC_API_KEY"):
        env.pop(name, None)

    def felix(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "felix", "--home", home, *args], env=env, capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL)

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
        # a terminal that cannot show bullets or emoji gets "?" for them, not a crash
        plain = subprocess.run([sys.executable, "-m", "felix", "--home", home, "ask", "what's on tomorrow"],
                               env={**env, "PYTHONIOENCODING": "ascii"}, capture_output=True, timeout=60,
                               stdin=subprocess.DEVNULL)
        assert plain.returncode == 0 and b"Traceback" not in plain.stderr
        assert b"10 AM?11:30 AM  Board prep" in plain.stdout and plain.stdout.isascii()
        paired = felix("pair")
        assert paired.returncode == 1 and "cannot pair a phone" in paired.stderr
        assert "REST OF TODAY" in felix("brief").stdout.upper() or "TOMORROW" in felix("brief").stdout.upper()
        # starting the demo a second time (a shortcut clicked twice) finds the first one
        again = felix("demo", "--no-browser", "--port", str(port))
        assert again.returncode == 0 and "already running" in again.stdout
        # and `felix stop` ends it cleanly
        stopped = felix("stop")
        assert stopped.returncode == 0 and "Felix has stopped." in stopped.stdout
        assert server.wait(timeout=20) == 0
        assert "Felix is not running." in felix("stop").stdout
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
