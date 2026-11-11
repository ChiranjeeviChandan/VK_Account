"""Laptop control: apps, files, volume, lock, screenshots, clipboard, shell.

Looking and harmless switches run straight away. Anything that writes files,
deletes, powers the machine down or runs a shell command waits for the
owner's approval, and so does reading files that look like credentials.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import socket
import subprocess
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..tools import DANGER, LOCAL, READ, Registry, ToolContext, ToolError
from ..util import clip
from . import platform as osmap

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix

log = logging.getLogger("felix.laptop")

READ_LIMIT = 200_000          # bytes of a text file handed back in one read
OUTPUT_LIMIT = 12_000         # characters of command output handed back

# Files whose contents are secrets. Reading them needs the owner's approval
# even though ordinary reads do not: a message that tricks a model into
# "summarising ~/.ssh/id_rsa" must hit a wall.
SENSITIVE_NAMES = (
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", "*.pem", "*.key", "*.p12", "*.pfx", "*.kdbx", "*.keystore",
    "*.jks", ".env", ".env.*", ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.*", "secrets.*",
    "*.ovpn", "login data", "cookies", "key4.db", "logins.json", "wallet.dat", "shadow", "master.key",
)
SENSITIVE_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", "gcloud", "keychains", "keyrings",
                  ".password-store", "1password", "bitwarden")
PUBLIC_EXCEPTIONS = ("*.pub", "known_hosts", "config", "authorized_keys")


class Laptop:
    def __init__(self, app: "Felix", os_name: str | None = None) -> None:
        self.app = app
        self.os = os_name or osmap.system()

    # -- running things -----------------------------------------------------------
    def _run(self, candidates: list[osmap.Command], input_text: str | None = None, timeout: float = 20,
             what: str = "that") -> str:
        """Run the first candidate whose program is installed. Returns stdout."""
        tried = []
        for command in candidates:
            if shutil.which(command[0]) is None:
                tried.append(command[0])
                continue
            try:
                result = subprocess.run(command, input=input_text, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise ToolError(f"{command[0]} took too long.") from None
            except OSError as exc:
                raise ToolError(f"Could not run {command[0]}: {exc}") from exc
            if result.returncode == 0:
                return result.stdout
            tried.append(f"{command[0]} ({clip(result.stderr.strip() or 'failed', 80)})")
        raise ToolError(f"This laptop has no way to do {what} (tried: {', '.join(tried) or 'nothing available'}).")

    def _spawn(self, candidates: list[osmap.Command], what: str) -> None:
        """Start something and do not wait for it (apps, viewers)."""
        for command in candidates:
            if shutil.which(command[0]) is None:
                continue
            try:
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return
            except OSError:
                continue
        raise ToolError(f"Could not open {what} on this laptop.")

    # -- actions used by other parts of Felix -----------------------------------------
    def notify(self, title: str, text: str) -> bool:
        """Desktop notification. Never raises: a brief must not fail because of it."""
        try:
            self._run(osmap.notify(self.os, title, text), timeout=15, what="notifications")
            return True
        except ToolError:
            return False

    def open(self, target: str) -> str:
        target = target.strip()
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", target) or re.match(r"^(?:www\.)?[\w-]+(\.[\w-]+)+(/\S*)?$", target):
            url = target if "://" in target else "https://" + target
            if not url.lower().startswith(("http://", "https://", "mailto:", "file://")):
                raise ToolError("I only open http, https, mailto and file links.")
            if not webbrowser.open(url):
                self._spawn(osmap.open_path(self.os, url), url)
            return f"Opened {url}."
        path = Path(target).expanduser()
        if path.exists():
            if self.os == "windows" and hasattr(os, "startfile"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                self._spawn(osmap.open_path(self.os, str(path)), str(path))
            return f"Opened {path}."
        return self._open_app(target)

    def _open_app(self, name: str) -> str:
        if self.os == "linux":
            desktop = self._desktop_entry(name)
            if desktop:
                self._spawn([["gtk-launch", desktop["id"]], desktop["exec"]], name)
                return f"Opened {desktop['name']}."
            binary = shutil.which(name) or shutil.which(name.lower().replace(" ", "-"))
            if binary:
                self._spawn([[binary]], name)
                return f"Opened {name}."
            raise ToolError(f"I can't find an app called {name!r} on this laptop.")
        if self.os == "windows":
            shortcut = self._start_menu_shortcut(name)
            if shortcut and hasattr(os, "startfile"):
                os.startfile(shortcut)  # type: ignore[attr-defined]
                return f"Opened {Path(shortcut).stem}."
        self._spawn(osmap.open_app(self.os, name), name)
        return f"Opened {name}."

    def _desktop_entry(self, name: str) -> dict[str, Any] | None:
        wanted = name.lower().strip()
        roots = [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
                 Path.home() / ".local/share/applications", Path("/var/lib/flatpak/exports/share/applications"),
                 Path("/var/lib/snapd/desktop/applications")]
        best: tuple[int, dict[str, Any]] | None = None
        for root in roots:
            if not root.is_dir():
                continue
            for entry in root.glob("*.desktop"):
                try:
                    text = entry.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                title = re.search(r"^Name=(.+)$", text, re.M)
                command = re.search(r"^Exec=(.+)$", text, re.M)
                if not title or not command or re.search(r"^NoDisplay=true$", text, re.M):
                    continue
                label = title.group(1).strip()
                score = 3 if label.lower() == wanted else 2 if label.lower().startswith(wanted) else \
                    1 if wanted in label.lower() or wanted in entry.stem.lower() else 0
                if score and (best is None or score > best[0]):
                    argv = [part for part in command.group(1).split() if not part.startswith("%")]
                    best = (score, {"id": entry.stem, "name": label, "exec": argv})
        return best[1] if best else None

    def _start_menu_shortcut(self, name: str) -> str | None:
        wanted = name.lower().strip()
        roots = [Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
                 Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs"]
        best: tuple[int, str] | None = None
        for root in roots:
            if not root.is_dir():
                continue
            for shortcut in root.rglob("*.lnk"):
                label = shortcut.stem.lower()
                score = 3 if label == wanted else 2 if label.startswith(wanted) else 1 if wanted in label else 0
                if score and (best is None or score > best[0]):
                    best = (score, str(shortcut))
        return best[1] if best else None

    def screenshot(self) -> Path:
        folder = Path(self.app.config.get("laptop.screenshot_dir") or self.app.paths.files / "screenshots").expanduser()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"screenshot-{time.strftime('%Y%m%d-%H%M%S')}.png"
        self._run(osmap.screenshot(self.os, str(path)), timeout=30, what="screenshots")
        if not path.exists():
            raise ToolError("The screenshot command ran but produced no file.")
        return path

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"system": self.os, "host": socket.gethostname(),
                               "time": self.app.clock.now().isoformat(timespec="minutes"),
                               "online": self.app.net.online, "mode": self.app.net.mode}
        try:
            usage = shutil.disk_usage(Path.home())
            out["disk_free_gb"] = round(usage.free / 1e9, 1)
            out["disk_total_gb"] = round(usage.total / 1e9, 1)
        except OSError:
            pass
        battery = self.battery()
        if battery:
            out.update(battery)
        return out

    def battery(self) -> dict[str, Any] | None:
        if self.os == "linux":
            for supply in sorted(Path("/sys/class/power_supply").glob("BAT*")):
                try:
                    percent = int((supply / "capacity").read_text().strip())
                    state = (supply / "status").read_text().strip().lower()
                    return {"battery": percent, "charging": state in ("charging", "full")}
                except (OSError, ValueError):
                    continue
            return None
        try:
            text = self._run(osmap.battery(self.os), timeout=10, what="battery status")
        except ToolError:
            return None
        match = re.search(r"(\d{1,3})%", text)
        if not match:
            return None
        charging = bool(re.search(r"AC Power|charging|charged|status=(?:2|6|7|8|9)\b", text, re.I)) and \
            not re.search(r"discharging", text, re.I)
        return {"battery": int(match.group(1)), "charging": charging}

    # -- files ---------------------------------------------------------------------
    def _path(self, raw: str) -> Path:
        if not raw or not raw.strip():
            raise ToolError("A path is needed.")
        return Path(os.path.expandvars(raw.strip())).expanduser().resolve()

    def is_sensitive(self, path: Path) -> bool:
        """True for credential stores and Felix's own private data."""
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            resolved = path
        home = self.app.paths.home.resolve()
        if resolved == home or home in resolved.parents:
            return True
        name = resolved.name.lower()
        parts = {p.lower() for p in resolved.parts}
        if parts & set(SENSITIVE_DIRS):
            return not any(fnmatch.fnmatch(name, pattern) for pattern in PUBLIC_EXCEPTIONS)
        return any(fnmatch.fnmatch(name, pattern) for pattern in SENSITIVE_NAMES)

    def list_dir(self, raw: str, limit: int = 200) -> dict[str, Any]:
        path = self._path(raw or "~")
        if not path.is_dir():
            raise ToolError(f"{path} is not a folder.")
        entries = []
        try:
            children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            raise ToolError(f"No permission to look inside {path}.") from None
        for child in children[:limit]:
            try:
                info = child.stat()
                entries.append({"name": child.name + ("/" if child.is_dir() else ""),
                                "size": info.st_size if child.is_file() else None,
                                "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(info.st_mtime))})
            except OSError:
                entries.append({"name": child.name})
        return {"folder": str(path), "entries": entries, "truncated": len(children) > limit}

    def read_text(self, raw: str, max_bytes: int = READ_LIMIT) -> dict[str, Any]:
        path = self._path(raw)
        if not path.is_file():
            raise ToolError(f"{path} is not a file.")
        size = path.stat().st_size
        with path.open("rb") as handle:
            data = handle.read(max_bytes)
        if b"\x00" in data[:4096]:
            raise ToolError(f"{path.name} is not a text file ({size} bytes); I can open it in its app instead.")
        return {"path": str(path), "size": size, "truncated": size > len(data),
                "text": data.decode("utf-8", errors="replace")}

    def find(self, pattern: str, root: str = "~", limit: int = 50, max_depth: int = 6) -> dict[str, Any]:
        """Find files by name. ``pattern`` is a glob (*.pdf) or a fragment."""
        base = self._path(root)
        if not base.is_dir():
            raise ToolError(f"{base} is not a folder.")
        glob = pattern if any(c in pattern for c in "*?[") else f"*{pattern}*"
        matches: list[dict[str, Any]] = []
        deadline = time.monotonic() + 8
        base_depth = len(base.parts)
        for folder, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", "Library")]
            if len(Path(folder).parts) - base_depth >= max_depth:
                dirs[:] = []
            for name in files + dirs:
                if fnmatch.fnmatch(name.lower(), glob.lower()):
                    full = Path(folder) / name
                    try:
                        modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(full.stat().st_mtime))
                    except OSError:
                        modified = ""
                    matches.append({"path": str(full), "modified": modified})
                    if len(matches) >= limit:
                        return {"matches": matches, "truncated": True}
            if time.monotonic() > deadline:
                return {"matches": matches, "truncated": True, "note": "search stopped after 8 seconds"}
        matches.sort(key=lambda m: m["modified"], reverse=True)
        return {"matches": matches, "truncated": False}

    def trash(self, path: Path) -> Path:
        """'Delete' by moving into Felix's own trash folder, so it can be undone."""
        bin_dir = self.app.paths.files / "trash" / time.strftime("%Y%m%d-%H%M%S")
        bin_dir.mkdir(parents=True, exist_ok=True)
        target = bin_dir / path.name
        shutil.move(str(path), str(target))
        return target

    def run_shell(self, command: str, cwd: str | None = None, timeout: float | None = None) -> dict[str, Any]:
        if not self.app.config.get("laptop.shell_enabled", True):
            raise ToolError("Shell commands are switched off in your settings (laptop.shell_enabled).")
        limit = float(timeout or self.app.config.get("laptop.shell_timeout", 60))
        folder = str(self._path(cwd)) if cwd else str(Path.home())
        started = time.monotonic()
        try:
            result = subprocess.run(command, shell=True, cwd=folder, capture_output=True, text=True,
                                    timeout=limit, errors="replace")
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
            raise ToolError(f"The command was stopped after {int(limit)} seconds. Output so far:\n"
                            f"{clip(partial, 2000)}") from None
        except OSError as exc:
            raise ToolError(f"Could not run the command: {exc}") from exc
        return {"exit_code": result.returncode, "seconds": round(time.monotonic() - started, 2),
                "output": _tail(result.stdout, OUTPUT_LIMIT),
                **({"errors": _tail(result.stderr, OUTPUT_LIMIT // 3)} if result.stderr.strip() else {})}

    # -- tools -----------------------------------------------------------------------
    def register(self, tools: Registry) -> None:
        @tools.tool(
            "laptop_status",
            "Report on the laptop Felix runs on: operating system, battery, free disk space, connection state.",
            {}, risk=READ, group="laptop",
        )
        def laptop_status(args: dict[str, Any], ctx: ToolContext) -> Any:
            return self.status()

        @tools.tool(
            "laptop_open",
            "Open something on the laptop: an application by name ('Excel', 'Chrome'), a file or folder by "
            "path, or a web address.",
            {"target": {"type": "string", "description": "App name, file path, or URL"}},
            ["target"], risk=LOCAL, channel="laptop_control", group="laptop",
        )
        def laptop_open(args: dict[str, Any], ctx: ToolContext) -> Any:
            return {"message": self.open(args["target"])}

        power = {"sleep"}

        @tools.tool(
            "laptop_control",
            "Change the laptop's state. Actions: volume (level 0-100), mute, unmute, lock (lock the screen), "
            "sleep (needs approval), screenshot (saves a PNG and returns its path), notify (show `text` as a "
            "desktop notification), clipboard_get, clipboard_set (`text`).",
            {
                "action": {"type": "string", "enum": ["volume", "mute", "unmute", "lock", "sleep", "screenshot",
                                                      "notify", "clipboard_get", "clipboard_set"]},
                "level": {"type": "integer", "minimum": 0, "maximum": 100},
                "text": {"type": "string"},
                "title": {"type": "string"},
            },
            ["action"], risk=LOCAL, group="laptop",
            channel=lambda args: "power" if args.get("action") in power else "laptop_control",
            extra={"risk_for": lambda args: DANGER if args.get("action") in power else LOCAL},
            preview=lambda args, ctx: f"Laptop: {args['action']}",
        )
        def laptop_control(args: dict[str, Any], ctx: ToolContext) -> Any:
            action = args["action"]
            if action == "volume":
                if "level" not in args:
                    raise ToolError("Give a volume level from 0 to 100.")
                self._run(osmap.set_volume(self.os, args["level"]), what="volume control")
                return {"message": f"Volume set to {args['level']}%."}
            if action in ("mute", "unmute"):
                self._run(osmap.set_mute(self.os, action == "mute"), what="muting")
                note = " (Windows only has a mute toggle)" if self.os == "windows" else ""
                return {"message": f"Sound {'muted' if action == 'mute' else 'on'}{note}."}
            if action == "lock":
                self._run(osmap.lock(self.os), what="locking the screen")
                return {"message": "Screen locked."}
            if action == "sleep":
                self._run(osmap.sleep(self.os), what="sleep")
                return {"message": "Going to sleep."}
            if action == "screenshot":
                path = self.screenshot()
                return {"message": f"Screenshot saved to {path}.", "path": str(path)}
            if action == "notify":
                if not args.get("text"):
                    raise ToolError("Give the text to show.")
                if not self.notify(args.get("title") or "Felix", args["text"]):
                    raise ToolError("This laptop has no notification command available.")
                return {"message": "Shown."}
            if action == "clipboard_get":
                text = self._run(osmap.clipboard_read(self.os), what="reading the clipboard")
                return {"text": clip(text, 4000)}
            if not args.get("text"):
                raise ToolError("Give the text to copy.")
            self._run(osmap.clipboard_write(self.os), input_text=args["text"], what="writing the clipboard")
            return {"message": "Copied to the clipboard."}

        def file_risk(args: dict[str, Any]) -> str:
            if args.get("action") != "read":
                return READ
            try:
                return DANGER if self.is_sensitive(self._path(args.get("path", ""))) else READ
            except ToolError:
                return READ

        @tools.tool(
            "laptop_files",
            "Look at files on the laptop. Actions: list (a folder; default is the home folder), read (a text "
            "file's contents), find (files whose name matches `pattern`, e.g. '*.xlsx' or 'budget', under "
            "`path`). Reading credential files (keys, .env, password stores) needs the owner's approval.",
            {
                "action": {"type": "string", "enum": ["list", "read", "find"]},
                "path": {"type": "string", "description": "Folder or file; ~ is the home folder"},
                "pattern": {"type": "string", "description": "For find: a glob or part of a file name"},
            },
            ["action"], risk=READ, channel="files_sensitive", group="laptop", extra={"risk_for": file_risk},
            preview=lambda args, ctx: f"Read a file that looks like a credential\n{self._path(args.get('path', ''))}",
        )
        def laptop_files(args: dict[str, Any], ctx: ToolContext) -> Any:
            action = args["action"]
            if action == "list":
                return self.list_dir(args.get("path") or "~")
            if action == "read":
                if not args.get("path"):
                    raise ToolError("Which file?")
                return self.read_text(args["path"])
            if not args.get("pattern"):
                raise ToolError("Give a file name pattern to look for.")
            return self.find(args["pattern"], args.get("path") or "~")

        @tools.tool(
            "laptop_file_change",
            "Change files on the laptop: write (create or replace a text file with `content`), append, mkdir, "
            "copy or move (`path` to `destination`), delete (moved to Felix's trash folder, so it can be "
            "restored). Every change needs the owner's approval.",
            {
                "action": {"type": "string", "enum": ["write", "append", "mkdir", "copy", "move", "delete"]},
                "path": {"type": "string"},
                "content": {"type": "string"},
                "destination": {"type": "string"},
            },
            ["action", "path"], risk=DANGER, group="laptop",
            channel=lambda args: "files_delete" if args.get("action") == "delete" else "files_write",
            preview=lambda args, ctx: _change_preview(self, args),
        )
        def laptop_file_change(args: dict[str, Any], ctx: ToolContext) -> Any:
            action = args["action"]
            path = self._path(args["path"])
            home = self.app.paths.home.resolve()
            if path == home or home in path.parents:
                raise ToolError("Felix's own data folder is off limits.")
            if action in ("write", "append"):
                if "content" not in args:
                    raise ToolError("Give the content to write.")
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a" if action == "append" else "w", encoding="utf-8") as handle:
                    handle.write(args["content"])
                return {"message": f"{'Appended to' if action == 'append' else 'Wrote'} {path} "
                                   f"({len(args['content'])} characters)."}
            if action == "mkdir":
                path.mkdir(parents=True, exist_ok=True)
                return {"message": f"Created folder {path}."}
            if not path.exists():
                raise ToolError(f"{path} does not exist.")
            if action == "delete":
                kept = self.trash(path)
                return {"message": f"Deleted {path} (kept in {kept} in case you want it back)."}
            if not args.get("destination"):
                raise ToolError("Give a destination.")
            destination = self._path(args["destination"])
            if destination.is_dir():
                destination = destination / path.name
            if destination.exists():
                raise ToolError(f"{destination} already exists; choose another name.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if action == "copy":
                if path.is_dir():
                    shutil.copytree(path, destination)
                else:
                    shutil.copy2(path, destination)
                return {"message": f"Copied {path} to {destination}."}
            shutil.move(str(path), str(destination))
            return {"message": f"Moved {path} to {destination}."}

        @tools.tool(
            "laptop_shell",
            "Run a shell command on the laptop and return its output. Always needs the owner's approval. "
            "Use it only when no dedicated tool fits (installing software, git, system administration).",
            {"command": {"type": "string"},
             "cwd": {"type": "string", "description": "Folder to run in; default is the home folder"},
             "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 900}},
            ["command"], risk=DANGER, channel="shell", group="laptop",
            preview=lambda args, ctx: "Run shell command\n" + args["command"]
            + (f"\nin {args['cwd']}" if args.get("cwd") else ""),
        )
        def laptop_shell(args: dict[str, Any], ctx: ToolContext) -> Any:
            return self.run_shell(args["command"], args.get("cwd"), args.get("timeout_seconds"))

        @tools.tool(
            "laptop_processes",
            "List what is running on the laptop, busiest first.",
            {"limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            risk=READ, group="laptop",
        )
        def laptop_processes(args: dict[str, Any], ctx: ToolContext) -> Any:
            text = self._run(osmap.processes(self.os), timeout=15, what="listing processes")
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            return {"processes": lines[: args.get("limit", 25) + 1]}


def _change_preview(laptop: Laptop, args: dict[str, Any]) -> str:
    action = args["action"]
    path = laptop._path(args["path"])
    if action in ("write", "append"):
        verb = "Append to file" if action == "append" else ("Replace file" if path.exists() else "Create file")
        return f"{verb}\n{path}\n---\n{clip(args.get('content', ''), 1200)}"
    if action in ("copy", "move"):
        return f"{action.capitalize()} file\n{path}\nto {laptop._path(args.get('destination', ''))}" \
            if args.get("destination") else f"{action.capitalize()} file\n{path}"
    return f"{'Delete' if action == 'delete' else 'Create folder'}\n{path}"


def _tail(text: str, limit: int) -> str:
    """Keep the end of long output: that is where results and errors are."""
    text = text or ""
    return text if len(text) <= limit else f"[... {len(text) - limit} earlier characters omitted ...]\n" + text[-limit:]
