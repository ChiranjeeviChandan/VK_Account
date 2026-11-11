"""How to ask each operating system to do things.

Everything here is a pure function from intent to command lines, so the
mapping for Windows, macOS and Linux can be checked on any machine. Each
function returns a list of candidate commands; the caller runs the first one
whose program exists.
"""

from __future__ import annotations

import sys

Command = list[str]


def system(name: str | None = None) -> str:
    name = name or sys.platform
    if name.startswith("win"):
        return "windows"
    if name == "darwin":
        return "mac"
    return "linux"


def _ps(script: str) -> Command:
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]


def _ps_quote(text: str) -> str:
    """A PowerShell single-quoted literal."""
    return "'" + text.replace("'", "''") + "'"


def _osa_quote(text: str) -> str:
    """An AppleScript string literal."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def open_path(os_name: str, path: str) -> list[Command]:
    if os_name == "windows":
        return [["cmd", "/c", "start", "", path]]
    if os_name == "mac":
        return [["open", path]]
    return [["xdg-open", path], ["gio", "open", path]]


def open_app(os_name: str, name: str) -> list[Command]:
    if os_name == "windows":
        return [["cmd", "/c", "start", "", name]]
    if os_name == "mac":
        return [["open", "-a", name]]
    return [["gtk-launch", name], [name]]


def set_volume(os_name: str, level: int) -> list[Command]:
    level = max(0, min(100, int(level)))
    if os_name == "windows":
        # No built-in volume CLI: press volume-down to the floor, then up. One key press is 2%.
        ups = round(level / 2)
        script = ("$w = New-Object -ComObject WScript.Shell; "
                  "1..50 | ForEach-Object { $w.SendKeys([char]174) }; "
                  + (f"1..{ups} | ForEach-Object {{ $w.SendKeys([char]175) }}" if ups else ""))
        return [_ps(script.strip().rstrip(";"))]
    if os_name == "mac":
        return [["osascript", "-e", f"set volume output volume {level}"]]
    return [["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"],
            ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{level / 100:.2f}"],
            ["amixer", "-q", "sset", "Master", f"{level}%"]]


def set_mute(os_name: str, muted: bool) -> list[Command]:
    if os_name == "windows":
        # The mute key is a toggle; Windows offers no way to read the state without extra software.
        return [_ps("(New-Object -ComObject WScript.Shell).SendKeys([char]173)")]
    if os_name == "mac":
        return [["osascript", "-e", f"set volume output muted {'true' if muted else 'false'}"]]
    flag = "1" if muted else "0"
    return [["pactl", "set-sink-mute", "@DEFAULT_SINK@", flag],
            ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", flag],
            ["amixer", "-q", "sset", "Master", "mute" if muted else "unmute"]]


def lock(os_name: str) -> list[Command]:
    if os_name == "windows":
        return [["rundll32.exe", "user32.dll,LockWorkStation"]]
    if os_name == "mac":
        return [["pmset", "displaysleepnow"]]
    return [["loginctl", "lock-session"], ["xdg-screensaver", "lock"], ["gnome-screensaver-command", "-l"]]


def sleep(os_name: str) -> list[Command]:
    if os_name == "windows":
        return [["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]]
    if os_name == "mac":
        return [["pmset", "sleepnow"]]
    return [["systemctl", "suspend"]]


def screenshot(os_name: str, path: str) -> list[Command]:
    if os_name == "windows":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            "$b = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height; "
            "$g = [System.Drawing.Graphics]::FromImage($bmp); "
            "$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size); "
            f"$bmp.Save({_ps_quote(path)}); $g.Dispose(); $bmp.Dispose()")
        return [_ps(script)]
    if os_name == "mac":
        return [["screencapture", "-x", path]]
    return [["gnome-screenshot", "-f", path], ["grim", path], ["spectacle", "-b", "-n", "-o", path],
            ["scrot", path], ["import", "-window", "root", path]]


def notify(os_name: str, title: str, text: str) -> list[Command]:
    if os_name == "windows":
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
            "$n = New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon = [System.Drawing.SystemIcons]::Information; $n.Visible = $true; "
            f"$n.ShowBalloonTip(10000, {_ps_quote(title)}, {_ps_quote(text[:250])}, "
            "[System.Windows.Forms.ToolTipIcon]::Info); Start-Sleep -Seconds 10; $n.Dispose()")
        return [_ps(script)]
    if os_name == "mac":
        return [["osascript", "-e", f"display notification {_osa_quote(text[:250])} with title {_osa_quote(title)}"]]
    return [["notify-send", "--app-name=Felix", title, text[:400]]]


def clipboard_read(os_name: str) -> list[Command]:
    if os_name == "windows":
        return [_ps("Get-Clipboard -Raw")]
    if os_name == "mac":
        return [["pbpaste"]]
    return [["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]


def clipboard_write(os_name: str) -> list[Command]:
    """Commands that read the new clipboard text from standard input."""
    if os_name == "windows":
        return [["clip"]]
    if os_name == "mac":
        return [["pbcopy"]]
    return [["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]


def battery(os_name: str) -> list[Command]:
    if os_name == "windows":
        return [_ps("$b = Get-CimInstance Win32_Battery | Select-Object -First 1; "
                    "if ($b) { \"$($b.EstimatedChargeRemaining)% status=$($b.BatteryStatus)\" }")]
    if os_name == "mac":
        return [["pmset", "-g", "batt"]]
    return []   # Linux: read /sys/class/power_supply directly


def processes(os_name: str) -> list[Command]:
    if os_name == "windows":
        return [["tasklist", "/FO", "CSV", "/NH"]]
    if os_name == "mac":
        return [["ps", "-Aceo", "pid,pcpu,pmem,comm", "-r"]]
    return [["ps", "-eo", "pid,pcpu,pmem,comm", "--sort=-pcpu"]]
