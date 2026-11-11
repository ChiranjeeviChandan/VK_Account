"""Laptop control: the per-OS command mapping, files, shell and their gates."""

import os
import sys
import threading
from pathlib import Path

import pytest

from felix.control import platform as osmap
from felix.control.laptop import Laptop
from felix.tools import ToolContext


def run(felix, tool, **args):
    result = felix.tools.execute(tool, args, ToolContext(felix, thread="t", source="test"))
    assert result.ok, result.error
    return result


# -- the command mapping is pure, so all three systems are checked everywhere ------------

def test_system_detection():
    assert (osmap.system("win32"), osmap.system("darwin"), osmap.system("linux")) == ("windows", "mac", "linux")


def test_volume_commands():
    assert osmap.set_volume("mac", 40) == [["osascript", "-e", "set volume output volume 40"]]
    assert osmap.set_volume("linux", 140)[0] == ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "100%"]
    windows = osmap.set_volume("windows", 30)[0]
    assert windows[0] == "powershell" and "[char]174" in windows[-1] and "1..15 | ForEach-Object" in windows[-1]
    assert "[char]175" not in osmap.set_volume("windows", 0)[0][-1]         # zero: only presses volume-down
    assert osmap.set_mute("mac", True) == [["osascript", "-e", "set volume output muted true"]]
    assert osmap.set_mute("linux", False)[0] == ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"]


def test_lock_sleep_and_open_commands():
    assert osmap.lock("windows") == [["rundll32.exe", "user32.dll,LockWorkStation"]]
    assert osmap.lock("mac") == [["pmset", "displaysleepnow"]]
    assert osmap.lock("linux")[0] == ["loginctl", "lock-session"]
    assert osmap.sleep("linux") == [["systemctl", "suspend"]] and osmap.sleep("mac") == [["pmset", "sleepnow"]]
    assert osmap.open_app("mac", "Microsoft Excel") == [["open", "-a", "Microsoft Excel"]]
    assert osmap.open_path("windows", r"C:\Docs\plan.pdf") == [["cmd", "/c", "start", "", r"C:\Docs\plan.pdf"]]
    assert osmap.open_path("linux", "/tmp/x")[0] == ["xdg-open", "/tmp/x"]


def test_quoting_cannot_be_broken_out_of():
    hostile = "it's \"quoted\"; rm -rf ~ `whoami` $(id)"
    mac = osmap.notify("mac", "Felix", hostile)[0]
    assert mac[:2] == ["osascript", "-e"] and '\\"quoted\\"' in mac[2]
    windows = osmap.notify("windows", "Felix", hostile)[0][-1]
    assert "'it''s \"quoted\"; rm -rf ~ `whoami` $(id)'" in windows          # one single-quoted literal
    linux = osmap.notify("linux", "Felix", hostile)[0]
    assert linux[-1] == hostile and linux[0] == "notify-send"               # passed as an argument, never a shell string
    shot = osmap.screenshot("windows", r"C:\Users\O'Brien\shot.png")[0][-1]
    assert r"'C:\Users\O''Brien\shot.png'" in shot


def test_clipboard_and_screenshot_candidates():
    assert osmap.clipboard_write("mac") == [["pbcopy"]] and osmap.clipboard_read("windows")[0][0] == "powershell"
    assert [c[0] for c in osmap.clipboard_write("linux")] == ["wl-copy", "xclip", "xsel"]
    assert osmap.screenshot("mac", "/tmp/s.png") == [["screencapture", "-x", "/tmp/s.png"]]
    assert [c[0] for c in osmap.screenshot("linux", "/tmp/s.png")][:2] == ["gnome-screenshot", "grim"]


# -- behaviour on this machine ----------------------------------------------------------

def test_status(app):
    status = run(app, "laptop_status").data
    assert status["system"] == osmap.system() and status["online"] is False and status["disk_free_gb"] > 0


def test_missing_programs_are_reported_not_crashed(app, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    result = app.tools.execute("laptop_control", {"action": "volume", "level": 30}, ToolContext(app))
    assert not result.ok and "no way to do volume control" in result.error
    assert app.laptop.notify("Felix", "hello") is False          # never raises


def test_runs_first_available_command(app, monkeypatch, tmp_path):
    marker = tmp_path / "ran.txt"
    script = tmp_path / "pactl"
    script.write_text(f"#!/bin/sh\necho \"$@\" > {marker}\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    laptop = Laptop(app, os_name="linux")
    laptop._run(osmap.set_volume("linux", 35), what="volume control")
    assert marker.read_text().strip() == "set-sink-volume @DEFAULT_SINK@ 35%"


def test_open_refuses_odd_schemes_and_unknown_apps(app):
    odd = app.tools.execute("laptop_open", {"target": "javascript://alert(1)"}, ToolContext(app))
    assert not odd.ok and "only open http" in odd.error
    if osmap.system() == "linux":
        missing = app.tools.execute("laptop_open", {"target": "No Such App 9000"}, ToolContext(app))
        assert not missing.ok and "can't find an app" in missing.error


def test_list_read_find(app, tmp_path):
    docs = tmp_path / "docs"
    (docs / "sub").mkdir(parents=True)
    (docs / "Budget 2026.xlsx").write_bytes(b"PK\x03\x04\x00\x00binary")
    (docs / "notes.txt").write_text("Call the landlord about the lease.\n")
    (docs / "sub" / "budget-draft.txt").write_text("draft")
    listing = run(app, "laptop_files", action="list", path=str(docs)).data
    assert [e["name"] for e in listing["entries"]] == ["sub/", "Budget 2026.xlsx", "notes.txt"]
    assert run(app, "laptop_files", action="read", path=str(docs / "notes.txt")).data["text"].startswith("Call the landlord")
    binary = app.tools.execute("laptop_files", {"action": "read", "path": str(docs / "Budget 2026.xlsx")}, ToolContext(app))
    assert not binary.ok and "not a text file" in binary.error
    found = run(app, "laptop_files", action="find", pattern="budget", path=str(docs)).data["matches"]
    assert {Path(m["path"]).name for m in found} == {"Budget 2026.xlsx", "budget-draft.txt"}
    assert [Path(m["path"]).name for m in run(app, "laptop_files", action="find", pattern="*.xlsx", path=str(docs)).data["matches"]] == ["Budget 2026.xlsx"]


def test_credential_files_need_approval_to_read(app, tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n")
    (ssh / "id_ed25519.pub").write_text("ssh-ed25519 AAAA public\n")
    (tmp_path / ".env").write_text("API_KEY=abc\n")
    for path in (ssh / "id_ed25519", tmp_path / ".env", app.paths.secrets, app.paths.db):
        assert app.laptop.is_sensitive(path), path
    for path in (ssh / "id_ed25519.pub", tmp_path / "report.txt", tmp_path / "keynote.pptx"):
        assert not app.laptop.is_sensitive(path), path
    asked = run(app, "laptop_files", action="read", path=str(ssh / "id_ed25519"))
    assert asked.pending and asked.pending["risk"] == "danger" and "looks like a credential" in asked.pending["preview"]
    assert not run(app, "laptop_files", action="read", path=str(ssh / "id_ed25519.pub")).pending
    # even "auto" cannot silently unlock credential reads
    app.config.set("policy.autonomy.files_sensitive", "auto")
    assert run(app, "laptop_files", action="read", path=str(tmp_path / ".env")).pending


def test_file_changes_wait_for_approval_and_delete_is_recoverable(app, tmp_path):
    target = tmp_path / "out" / "memo.txt"
    asked = run(app, "laptop_file_change", action="write", path=str(target), content="Draft memo")
    assert asked.pending["preview"] == f"Create file\n{target}\n---\nDraft memo" and not target.exists()
    app.policy.approve(asked.pending["id"], via="test")
    assert target.read_text() == "Draft memo"
    app.policy.approve(run(app, "laptop_file_change", action="append", path=str(target), content="\nMore").pending["id"], via="test")
    assert target.read_text() == "Draft memo\nMore"
    moved = tmp_path / "archive" / "memo-old.txt"
    app.policy.approve(run(app, "laptop_file_change", action="move", path=str(target), destination=str(moved)).pending["id"], via="test")
    assert moved.exists() and not target.exists()
    gone = run(app, "laptop_file_change", action="delete", path=str(moved))
    assert gone.pending["channel"] == "files_delete"
    message = app.policy.approve(gone.pending["id"], via="test").data["message"]
    assert not moved.exists() and "in case you want it back" in message
    kept = list((app.paths.files / "trash").rglob("memo-old.txt"))
    assert len(kept) == 1 and kept[0].read_text() == "Draft memo\nMore"
    # Felix's own data cannot be overwritten through this tool, even when approved
    own = run(app, "laptop_file_change", action="write", path=str(app.paths.config), content="{}")
    assert "off limits" in app.policy.approve(own.pending["id"], via="test").error


def test_shell_needs_approval_runs_and_times_out(app, tmp_path):
    asked = run(app, "laptop_shell", command=f'{sys.executable} -c "print(6*7)"', cwd=str(tmp_path))
    assert asked.pending["preview"].startswith("Run shell command\n") and asked.pending["risk"] == "danger"
    done = app.policy.approve(asked.pending["id"], via="test")
    assert done.data["exit_code"] == 0 and done.data["output"].strip() == "42"
    failing = run(app, "laptop_shell", command=f'{sys.executable} -c "import sys; sys.stderr.write(chr(98)+chr(97)+chr(100)); sys.exit(3)"')
    result = app.policy.approve(failing.pending["id"], via="test").data
    assert result["exit_code"] == 3 and result["errors"] == "bad"
    slow = run(app, "laptop_shell", command=f'{sys.executable} -c "import time; time.sleep(5)"', timeout_seconds=1)
    assert "stopped after 1 seconds" in app.policy.approve(slow.pending["id"], via="test").error
    app.config.set("laptop.shell_enabled", False)
    off = run(app, "laptop_shell", command="echo hi")
    assert "switched off" in app.policy.approve(off.pending["id"], via="test").error


def test_sleep_needs_approval_but_lock_does_not(app, monkeypatch):
    ran = []
    monkeypatch.setattr(Laptop, "_run", lambda self, candidates, **kw: ran.append(candidates[0]) or "")
    assert not run(app, "laptop_control", action="lock").pending and len(ran) == 1
    asked = run(app, "laptop_control", action="sleep")
    assert asked.pending["channel"] == "power" and len(ran) == 1
    assert not run(app, "laptop_control", action="clipboard_set", text="copied").pending


# -- outbox regression: one item, two deliverers ------------------------------------------

def test_concurrent_delivery_sends_once(app):
    calls, gate = [], threading.Event()

    def handler(payload):
        calls.append(payload)
        gate.wait(2)

    app.outbox.handler("probe", handler)
    item = app.outbox.enqueue("probe", {"n": 1}, "probe", send_now=False)
    threads = [threading.Thread(target=app.outbox._deliver, args=(item,)) for _ in range(4)]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join()
    assert len(calls) == 1 and app.outbox.get(item["id"])["status"] == "sent"


def test_items_caught_mid_send_are_requeued_on_restart(app):
    app.outbox.handler("probe", lambda payload: None)
    item = app.outbox.enqueue("probe", {"n": 1}, "probe", send_now=False)
    app.store.update("outbox", {"status": "sending"}, "id = ?", (item["id"],))
    assert app.outbox.recover() == 1 and app.outbox.flush()["sent"] == 1
