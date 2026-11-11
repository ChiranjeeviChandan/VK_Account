"""The `felix` command.

    felix start            run Felix (dashboard, scheduler, phone link)
    felix                  chat in the terminal
    felix ask "..."        one question, one answer
    felix setup            first-time setup
    felix demo             try Felix with sample data, no accounts needed
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import signal
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .client import CoreClient, CoreUnavailable
from .config import ConfigError
from .paths import Paths, default_home
from .tools import ToolError
from .util import plural


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="felix", description="Felix - your private executive assistant.")
    parser.add_argument("--home", help="data folder (default: per-user application data)")
    parser.add_argument("--version", action="version", version=f"felix {__version__}")
    commands = parser.add_subparsers(dest="command")

    start = commands.add_parser("start", help="run Felix")
    start.add_argument("--port", type=int)
    start.add_argument("--no-browser", action="store_true")
    start.add_argument("--verbose", action="store_true")

    ask = commands.add_parser("ask", help="ask one question")
    ask.add_argument("text", nargs="+")
    commands.add_parser("chat", help="chat in the terminal")
    commands.add_parser("setup", help="first-time setup")
    commands.add_parser("status", help="show what is connected")
    commands.add_parser("doctor", help="check the installation")
    commands.add_parser("open", help="open the dashboard")
    commands.add_parser("pair", help="pair your Android phone")
    commands.add_parser("overlay", help="open the floating command bar")
    commands.add_parser("pending", help="list actions awaiting approval")

    for name in ("approve", "deny"):
        decide = commands.add_parser(name, help=f"{name} a pending action")
        decide.add_argument("id", type=int)

    brief = commands.add_parser("brief", help="show or send the brief")
    brief.add_argument("kind", nargs="?", default="now", choices=["morning", "evening", "now"])
    brief.add_argument("--send", action="store_true", help="deliver it to your WhatsApp")

    mode = commands.add_parser("mode", help="auto, online or offline")
    mode.add_argument("mode", choices=["auto", "online", "offline"])

    config = commands.add_parser("config", help="read or change a setting")
    config.add_argument("action", choices=["get", "set"])
    config.add_argument("key")
    config.add_argument("value", nargs="?")

    connect = commands.add_parser("connect", help="connect an account")
    connect.add_argument("what", choices=["claude", "mail", "google", "microsoft", "calendar", "whatsapp"])
    connect.add_argument("value", nargs="?", help="for calendar: an ICS address")

    autostart = commands.add_parser("autostart", help="start Felix when you log in")
    autostart.add_argument("action", choices=["enable", "disable"])

    demo = commands.add_parser("demo", help="run with sample data in a separate folder")
    demo.add_argument("--port", type=int, default=8790)
    demo.add_argument("--no-browser", action="store_true")

    args = parser.parse_args(argv)
    try:
        handler = HANDLERS[args.command or "chat"]
        return handler(args) or 0
    except CoreUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ToolError, ConfigError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:
        # Whoever was reading our output stopped (`felix ask ... | head`). Not worth a traceback.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 0


# -- helpers -----------------------------------------------------------------------

def _app(args: argparse.Namespace):
    from .app import Felix, configure_logging

    app = Felix(home=args.home)
    configure_logging(app.paths, verbose=getattr(args, "verbose", False))
    return app


def _client(args: argparse.Namespace) -> CoreClient:
    return CoreClient(args.home)


def _prompt(label: str, default: str = "", secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    try:
        value = getpass.getpass(f"{label}: ") if secret else input(f"{label}{suffix}: ")
    except EOFError:
        value = ""
    return value.strip() or default


def _yes(label: str, default: bool = True) -> bool:
    answer = _prompt(f"{label} ({'Y/n' if default else 'y/N'})").lower()
    return default if not answer else answer.startswith("y")


def _answer(args: argparse.Namespace, text: str, thread: str = "cli") -> dict[str, Any]:
    """Send one message to the running Core, or answer in-process when none is running."""
    client = _client(args)
    if client.running():
        result = client.chat(text, thread, on_delta=lambda piece: print(piece, end="", flush=True))
        print()
        return result
    app = _app(args)
    try:
        result = app.brain.respond(text, thread=thread, source="cli",
                                   emit=lambda e: e["type"] == "delta" and print(e["text"], end="", flush=True))
        print()
        return result
    finally:
        app.close()


def _wait_for_exit() -> None:
    stop = threading.Event()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), lambda *_: stop.set())
            except ValueError:      # not the main thread
                pass
    while not stop.wait(0.5):
        pass


# -- commands ----------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    app = _app(args)
    try:
        app.start(port=args.port)
    except OSError as exc:
        print(f"Felix could not start: {exc}. Is it already running? Try: felix status", file=sys.stderr)
        app.close()
        return 1
    link = app.server.url(with_token=True)
    engine = {"claude": "Claude (online)", "local": "local model", "rules": "offline commands"}[app.brain.engine_name()]
    print(f"Felix {__version__} is running.")
    print(f"  Dashboard : {app.server.url()}   (open it with: felix open)")
    print(f"  Brain     : {engine}")
    print(f"  Data      : {app.paths.home}")
    for kind, when in app.scheduler.next_briefs().items():
        print(f"  Next {kind} brief: {when.replace('T', ' ')[:16]}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser and app.config.get("ui.open_browser", True):
        try:
            webbrowser.open(link)
        except Exception:
            pass
    try:
        _wait_for_exit()
    finally:
        print("\nStopping Felix...")
        app.close()
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    result = _answer(args, " ".join(args.text))
    _show_approvals(result)
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    client = _client(args)
    running = client.running()
    app = None if running else _app(args)
    print(f"Felix {__version__}" + ("" if running else " (Core is not running: answering in this terminal)")
          + '. Type "help" for examples, "exit" to leave.')
    try:
        while True:
            try:
                text = input("\nyou> ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text.lower() in ("exit", "quit", "bye"):
                break
            print("felix> ", end="", flush=True)
            if running:
                result = client.chat(text, "cli", on_delta=lambda piece: print(piece, end="", flush=True))
            else:
                assert app is not None
                result = app.brain.respond(text, thread="cli", source="cli",
                                           emit=lambda e: e["type"] == "delta" and print(e["text"], end="", flush=True))
            print()
            _show_approvals(result)
    finally:
        if app is not None:
            app.close()
    return 0


def _show_approvals(result: dict[str, Any]) -> None:
    for approval in result.get("approvals") or []:
        print(f"\n  [waiting for you: #{approval['id']}] reply \"yes\" to go ahead, \"no\" to cancel")


def cmd_status(args: argparse.Namespace) -> int:
    client = _client(args)
    if not client.running():
        print(f"Felix is not running. Data folder: {Paths(args.home).home}\nStart it with: felix start")
        return 2
    status = client.get("/api/status")
    net, brain, phone = status["net"], status["brain"], status["phone"]
    print(f"Felix {status['version']} is running for {status['owner'] or 'you'}.")
    print(f"  Connection : {'online' if net['online'] else 'offline'} (mode {net['mode']})")
    engine = {"claude": f"Claude ({brain['model']})", "local": f"local model ({brain['local_model']})",
              "rules": "offline commands (no language model connected)"}[brain["engine"]]
    print(f"  Brain      : {engine}")
    print("  Phone      : " + ("connected" if phone["connected"] else "paired, not reachable" if phone["paired"] else "not paired"))
    print(f"  Mail       : {plural(len(status['mail']['accounts']), 'account')}, {status['mail']['unread']} unread")
    routes = status["whatsapp"]
    ready = [name for name in ("bridge", "phone", "cloud") if routes[name].get("ready")]
    print(f"  WhatsApp   : {', '.join(ready) if ready else 'no route available'}")
    counts = status["counts"]
    print(f"  Waiting    : {plural(counts['approvals'], 'approval')}, {plural(counts['outbox'], 'queued message')}")
    for kind, when in status["next_briefs"].items():
        print(f"  Next {kind} brief: {when.replace('T', ' ')[:16]}")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    client = _client(args)
    if not client.running():
        raise CoreUnavailable("Felix is not running. Start it with: felix start")
    link = f"{client.base}/login?token={client.token}"
    if not webbrowser.open(link):
        print(f"Open this link in your browser:\n{link}")
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    pairing = _client(args).post("/api/pair/new")
    print("Pair your Android phone with Felix\n")
    print("1. Install the Felix Companion app on the phone (see android/README.md).")
    print("2. Scan this code with the phone's camera, or open the app and type the details below.\n")
    try:
        import segno

        segno.make(pairing["link"], error="m").terminal(compact=True, border=1)
    except Exception:
        print("   (install the 'segno' package to see a QR code here)")
    print(f"\n   Address : {', '.join(pairing['hosts'])}  port {pairing['port']} ({pairing['scheme']})")
    print(f"   Code    : {pairing['code']}   (valid for {pairing['expires_in'] // 60} minutes, single use)")
    if pairing.get("fingerprint"):
        pin = pairing["fingerprint"]
        print(f"   Check   : {pin[:16]} ... {pin[-16:]}")
    print("\nThe phone and laptop must be on the same network (or reachable through a VPN such as Tailscale).")
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    waiting = _client(args).get("/api/approvals")["approvals"]
    if not waiting:
        print("Nothing is waiting for your approval.")
    for item in waiting:
        print(f"#{item['id']}  {item['preview']}\n")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    print(_client(args).post(f"/api/approvals/{args.id}/approve")["message"])
    return 0


def cmd_deny(args: argparse.Namespace) -> int:
    _client(args).post(f"/api/approvals/{args.id}/deny")
    print(f"Cancelled #{args.id}.")
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.send:
        result = client.post("/api/brief/send", {"kind": args.kind})
        print(result["text"])
        print(f"\nSent via {result['via']}." if result["via"] else "\nNot delivered yet: " + "; ".join(result["errors"][:3]))
        return 0
    if client.running():
        result = _answer(args, {"morning": "morning brief", "evening": "evening summary", "now": "brief me"}[args.kind])
        return 0 if result else 1
    app = _app(args)
    try:
        print(app.briefing.render(app.briefing.gather(args.kind), style="plain"))
    finally:
        app.close()
    return 0


def cmd_mode(args: argparse.Namespace) -> int:
    client = _client(args)
    if client.running():
        state = client.post("/api/mode", {"mode": args.mode})
        print(f"Mode {state['mode']}: Felix is {'online' if state['online'] else 'offline'}.")
        return 0
    app = _app(args)
    try:
        app.set_mode(args.mode)
        print(f"Mode set to {args.mode}.")
    finally:
        app.close()
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    app = _app(args)
    try:
        if args.action == "get":
            value = app.config.get(args.key)
            print(json.dumps(value, indent=2, ensure_ascii=False) if isinstance(value, (dict, list)) else value)
            return 0
        if args.value is None:
            raise ToolError("Give a value: felix config set <key> <value>")
        try:
            value: Any = json.loads(args.value)
        except ValueError:
            value = args.value
        app.config.set(args.key, value)
        print(f"{args.key} = {json.dumps(value)}   (restart Felix if it is running)")
    finally:
        app.close()
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    app = _app(args)
    try:
        print("Felix setup. Press Enter to keep the value in brackets.\n")
        owner = app.config.get("owner", {})
        name = _prompt("Your first name", owner.get("name", ""))
        email = _prompt("Your email address", owner.get("email", ""))
        phone = _prompt("Your WhatsApp number with country code (for briefs)", owner.get("phone", ""))
        zone = _prompt("Your time zone (for example Asia/Kolkata)", owner.get("timezone", "") or _guess_zone())
        app.config.update({"owner": {"name": name, "email": email, "phone": phone, "timezone": zone}})
        morning = _prompt("Morning brief time", app.config.get("briefs.morning.time"))
        evening = _prompt("Evening brief time", app.config.get("briefs.evening.time"))
        app.config.update({"briefs": {"morning": {"time": morning}, "evening": {"time": evening}}})
        print("\nOnline brain: Felix uses Claude when connected. An API key comes from https://console.anthropic.com")
        if app.secrets.has("anthropic_api_key"):
            print("  A Claude API key is already saved.")
        elif _yes("Add a Claude API key now?"):
            _connect_claude(app)
        if not app.mail.accounts() and email and _yes("\nConnect your email now?"):
            _connect_mail(app, email)
        print("\nDone. Next steps:")
        print("  felix start      run Felix and open the dashboard")
        print("  felix pair       pair your Android phone (WhatsApp, calls, overlay)")
        print("  felix doctor     check that everything is in place")
    finally:
        app.close()
    return 0


def _guess_zone() -> str:
    env = os.environ.get("TZ", "")
    if "/" in env:
        return env
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return ""


def _connect_claude(app) -> None:
    key = _prompt("Claude API key (hidden)", secret=True)
    if not key:
        print("  No key entered.")
        return
    app.secrets.set("anthropic_api_key", key)
    print(f"  Saved to the {app.secrets.backend}.")
    if not app.net.online:
        print("  Offline: the key will be used when a connection is available.")
        return
    try:
        import anthropic

        model = app.brain.claude.model()
        app.brain.claude.client().models.retrieve(model)
        print(f"  Key works; Felix will use {model}.")
    except ImportError:
        print("  The 'anthropic' package is not installed: pip install anthropic")
    except anthropic.AuthenticationError:
        print("  Claude rejected that key. Check it and run: felix connect claude")
    except anthropic.APIError as exc:
        print(f"  Could not verify the key right now ({getattr(exc, 'message', exc)}). It is saved.")


def _connect_mail(app, address: str | None = None) -> None:
    from .connectors.mail import preset_for

    address = address or _prompt("Email address")
    preset = preset_for(address) or {}
    raw: dict[str, Any] = {"email": address}
    if not preset:
        raw["imap_host"] = _prompt("Incoming (IMAP) server")
        raw["imap_port"] = int(_prompt("IMAP port", "993"))
        raw["smtp_host"] = _prompt("Outgoing (SMTP) server")
        raw["smtp_port"] = int(_prompt("SMTP port", "465"))
        raw["smtp_security"] = "ssl" if raw["smtp_port"] == 465 else "starttls"
    raw["username"] = _prompt("Username", address)
    if "gmail" in preset.get("imap_host", ""):
        print("  Gmail needs an app password (not your normal password):")
        print("  https://myaccount.google.com/apppasswords  - requires 2-step verification.")
    elif "office365" in preset.get("imap_host", ""):
        print("  Microsoft accounts no longer accept passwords for mail apps. Use: felix connect microsoft")
    password = _prompt("Password or app password (hidden)", secret=True)
    account = app.mail.add_account(raw, password)
    if not app.net.online:
        print(f"  Saved {account.email}. Offline now; it will sync when connected.")
        return
    check = app.mail.check(account)
    if check["ok"]:
        result = app.mail.sync(account.id)
        print(f"  Connected. {result['new']} recent emails synced.")
    else:
        for half in ("imap", "smtp"):
            if check[half] != "ok":
                print(f"  {half.upper()} problem: {check[half]}")
        print("  The account is saved; fix the issue and run: felix connect mail")


def cmd_connect(args: argparse.Namespace) -> int:
    app = _app(args)
    try:
        if args.what == "claude":
            _connect_claude(app)
        elif args.what == "mail":
            _connect_mail(app)
        elif args.what == "calendar":
            url = args.value or _prompt("Calendar address (an .ics link)")
            source = app.calendar.add_source({"kind": "ics", "url": url, "name": _prompt("Name for this calendar", "Calendar")})
            result = app.calendar.sync(source["id"]) if app.net.online else {"events": 0, "errors": ["offline"]}
            print(f"  Added. {result['events']} events." + (f" ({'; '.join(result['errors'])})" if result["errors"] else ""))
        elif args.what == "google":
            _connect_google(app)
        elif args.what == "microsoft":
            _connect_microsoft(app)
        else:
            _connect_whatsapp(app)
    finally:
        app.close()
    return 0


def _connect_google(app) -> None:
    from .connectors.oauth import GOOGLE

    print("Google sign-in uses your own OAuth client, so tokens are issued to you and nobody else.")
    print("Create one (type: Desktop app) as described in docs/SETUP.md, then paste its details.\n")
    client_id = _prompt("OAuth client ID")
    client_secret = _prompt("OAuth client secret (hidden)", secret=True)
    account = _prompt("Google account email", app.config.get("owner.email", ""))
    app.oauth.set_client("google", client_id, client_secret)
    want_mail = _yes("Use this sign-in for Gmail too (instead of an app password)?", default=False)
    scopes = [GOOGLE["scopes"]["calendar"]] + ([GOOGLE["scopes"]["mail"]] if want_mail else [])
    print("\nA browser window will open for Google's consent screen...")
    app.oauth.google_sign_in(account, scopes, on_url=lambda url: print(f"If it does not open, visit:\n{url}\n"))
    source = app.calendar.add_source({"id": "google", "kind": "google", "name": "Google Calendar", "account": account})
    app.config.set("calendar.default_source", source["id"])
    if want_mail:
        app.mail.add_account({"email": account, "auth": "oauth_google"})
    result = app.calendar.sync("google")
    print(f"Signed in. {result['events']} calendar events synced." + (f" ({'; '.join(result['errors'])})" if result["errors"] else ""))


def _connect_microsoft(app) -> None:
    from .connectors.oauth import MICROSOFT

    print("Microsoft sign-in needs an app registration (see docs/SETUP.md). Paste its details.\n")
    client_id = _prompt("Application (client) ID")
    tenant = _prompt("Directory (tenant) ID", "common")
    account = _prompt("Microsoft account email", app.config.get("owner.email", ""))
    app.oauth.set_client("microsoft", client_id, tenant=tenant)
    device = app.oauth.microsoft_begin(MICROSOFT["scopes"]["mail"])
    print(f"\nOpen {device.get('verification_uri')} and enter the code: {device.get('user_code')}\nWaiting...")
    app.oauth.microsoft_finish(account, device)
    app.mail.add_account({"email": account, "auth": "oauth_microsoft"})
    print("Signed in. Mail will sync on the next start (or run: felix ask \"check mail\").")


def _connect_whatsapp(app) -> None:
    print("""Felix can reach WhatsApp three ways. They can be combined.

1. Phone app (recommended to start): pair the Felix Companion app. It reads WhatsApp
   notifications and replies through them. Nothing unofficial; needs the phone nearby.
      felix pair

2. Linked device: Felix connects like WhatsApp Web and can read and send everything,
   including briefs to your own "Message yourself" chat while the phone is locked.
   This uses an unofficial client, which WhatsApp's terms do not allow; accounts that
   send bulk or automated messages have been banned. Read docs/WHATSAPP.md first.

3. WhatsApp Business Cloud API (official): send-only, for briefs to your own number
   from a business number you control. Setup is in docs/WHATSAPP.md.
""")
    if _yes("Enable the linked-device bridge (option 2)?", default=False):
        bridge = app.whatsapp.bridge
        if bridge.installed():
            print("  The bridge is already installed.")
        else:
            print("  Downloading the bridge's packages with npm (once; needs the internet) ...")
            ok, detail = bridge.install()
            if not ok:
                print(f"  Could not install the bridge: {detail}")
                return
        app.config.set("whatsapp.bridge.enabled", True)
        print("  Enabled. Start Felix (felix start), open Settings > WhatsApp and scan the code with your phone:\n"
              "  WhatsApp > Linked devices > Link a device. It will be listed there as Chrome.")


def cmd_doctor(args: argparse.Namespace) -> int:
    app = _app(args)
    problems = 0

    def check(label: str, ok: bool, detail: str = "", required: bool = True) -> None:
        nonlocal problems
        mark = "ok  " if ok else ("FAIL" if required else "--  ")
        if not ok and required:
            problems += 1
        print(f"  [{mark}] {label}" + (f" - {detail}" if detail else ""))

    try:
        print(f"Felix {__version__}   data folder: {app.paths.home}\n")
        if app.sandbox:
            print("  This is the demo folder: sample data, and nothing is ever really sent.\n")
        check("Python 3.10 or newer", sys.version_info >= (3, 10), sys.version.split()[0])
        check("Database with full-text search", app.store.fts, "" if app.store.fts else "falling back to slower search", False)
        check("Secret storage", True, app.secrets.backend)
        for module, purpose in (("anthropic", "online brain"), ("cryptography", "phone link encryption"),
                                ("segno", "pairing QR code"), ("keyring", "OS keychain")):
            try:
                __import__(module)
                check(f"Package {module}", True, purpose)
            except ImportError:
                check(f"Package {module}", False, f"{purpose}: pip install {module}", required=False)
        online = app.net.reachable(force=True)
        check("Internet connection", online, f"mode {app.net.mode}", required=False)
        check("Claude API key", app.brain.claude.available(), "" if app.brain.claude.available() else "felix connect claude", False)
        check("Local model (Ollama)", app.brain.local.available(),
              f"{app.brain.local.config().get('model')} at {app.brain.local.url()}", required=False)
        accounts = app.mail.accounts()
        check("Email account", bool(accounts), ", ".join(a.email for a in accounts) or "felix connect mail", False)
        if accounts and online and not app.sandbox:
            for account in accounts:
                result = app.mail.check(account)
                detail = "" if result["ok"] else "; ".join(str(result[h]) for h in ("imap", "smtp") if result[h] != "ok")
                check(f"  {account.email} sign-in", bool(result["ok"]), detail)
        check("Owner WhatsApp number", bool(app.config.get("owner.phone")), "needed to send briefs to yourself", False)
        paired = app.phone.status()["paired"]
        check("Phone paired", paired, "" if paired else "felix pair", False)
        check("Node.js (WhatsApp bridge only)", bool(shutil.which("node")), "", required=False)
        check("WhatsApp bridge installed", app.whatsapp.bridge.installed(), "optional: felix connect whatsapp", False)
        briefs = app.scheduler.next_briefs()
        check("Brief schedule", bool(briefs), ", ".join(f"{k} {v[11:16]}" for k, v in briefs.items()), False)
        print("\nEverything required is in place." if not problems else f"\n{plural(problems, 'problem needs', 'problems need')} attention.")
    finally:
        app.close()
    return 1 if problems else 0


def autostart_file(action: str, platform: str | None = None, home: Path | None = None,
                   python: str | None = None) -> tuple[Path, str | None, str]:
    """Where the login-start file goes, its contents (None = delete) and the command to activate it."""
    platform = platform or sys.platform
    home = home or Path.home()
    python = python or sys.executable
    if platform.startswith("win"):
        folder = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Microsoft/Windows/Start Menu/Programs/Startup"
        runner = python.replace("python.exe", "pythonw.exe")
        body = f'@echo off\r\nstart "" "{runner}" -m felix start --no-browser\r\n'
        return folder / "Felix.cmd", body if action == "enable" else None, "(runs at your next sign-in)"
    if platform == "darwin":
        path = home / "Library/LaunchAgents/ai.felix.core.plist"
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ai.felix.core</string>
  <key>ProgramArguments</key><array>
    <string>{python}</string><string>-m</string><string>felix</string><string>start</string><string>--no-browser</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
"""
        command = f"launchctl {'load -w' if action == 'enable' else 'unload'} {path}"
        return path, body if action == "enable" else None, command
    path = home / ".config/systemd/user/felix.service"
    body = f"""[Unit]
Description=Felix assistant
After=network-online.target

[Service]
ExecStart={python} -m felix start --no-browser
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""
    command = "systemctl --user enable --now felix" if action == "enable" else "systemctl --user disable --now felix"
    return path, body if action == "enable" else None, command


def cmd_autostart(args: argparse.Namespace) -> int:
    path, body, command = autostart_file(args.action)
    if body is None:
        if path.exists():
            path.unlink()
        print(f"Removed {path}.\nIf Felix is still running in the background, run: {command}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    print(f"Wrote {path}.\nTo activate it now, run: {command}")
    return 0


def cmd_overlay(args: argparse.Namespace) -> int:
    from .overlay import run

    return run(_client(args))


def cmd_demo(args: argparse.Namespace) -> int:
    from .app import Felix, configure_logging
    from .demo import seed

    home = Path(args.home) if args.home else default_home().with_name(default_home().name + "-demo")
    fresh = not (home / "felix.db").exists()
    app = Felix(home=home, config={"sandbox": True, "owner": {"name": "Vikram"}, "server": {"port": args.port},
                                   "briefs": {"channels": ["desktop"]}} if fresh else None)
    configure_logging(app.paths)
    if fresh:
        app.config.save()
        seed(app)
    app.start(port=args.port, background_jobs=False)
    print("Felix demo is running with sample data (nothing is sent anywhere).")
    print(f"  Dashboard: {app.server.url(with_token=True)}")
    print(f"  Data     : {home}   (delete this folder to reset)")
    print('  Try      : "what\'s broken", "brief me", "who is waiting on me", "what\'s on today"')
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        try:
            webbrowser.open(app.server.url(with_token=True))
        except Exception:
            pass
    try:
        _wait_for_exit()
    finally:
        app.close()
    return 0


HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "start": cmd_start, "ask": cmd_ask, "chat": cmd_chat, "setup": cmd_setup, "status": cmd_status,
    "doctor": cmd_doctor, "open": cmd_open, "pair": cmd_pair, "overlay": cmd_overlay, "pending": cmd_pending,
    "approve": cmd_approve, "deny": cmd_deny, "brief": cmd_brief, "mode": cmd_mode, "config": cmd_config,
    "connect": cmd_connect, "autostart": cmd_autostart, "demo": cmd_demo,
}

if __name__ == "__main__":
    raise SystemExit(main())
