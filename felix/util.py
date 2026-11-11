"""Small shared helpers: clock, JSON, text and atomic file writes."""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone, tzinfo
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

UTC = timezone.utc


def resolve_tz(name: str | None) -> tzinfo:
    """Return a tzinfo for an IANA name, falling back to the system zone.

    Windows ships no IANA database, so an unknown name degrades to the local
    fixed offset instead of raising.
    """
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            pass
    local = datetime.now().astimezone().tzinfo
    return local or UTC


class Clock:
    """Source of "now". Tests replace it with a fixed or steppable clock."""

    def __init__(self, tz: tzinfo | None = None) -> None:
        self.tz: tzinfo = tz or resolve_tz(None)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def ts(self) -> float:
        return self.now().timestamp()

    def today(self):
        return self.now().date()

    def from_ts(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self.tz)


class FixedClock(Clock):
    def __init__(self, moment: datetime) -> None:
        super().__init__(moment.tzinfo or UTC)
        self._now = moment if moment.tzinfo else moment.replace(tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment if moment.tzinfo else moment.replace(tzinfo=self.tz)

    def advance(self, **kwargs: float) -> None:
        self._now = self._now + timedelta(**kwargs)


def json_dumps(value: Any, **kwargs: Any) -> str:
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("default", _json_default)
    return json.dumps(value, **kwargs)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def json_loads(text: str | bytes | None, default: Any = None) -> Any:
    if text in (None, "", b""):
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


def atomic_write(path: Path, data: str | bytes, private: bool = False) -> None:
    """Write a file so that a crash never leaves a half-written copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, mode, **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as fh:
            fh.write(data)
        if private and os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_WS = re.compile(r"\s+")


def squash(text: str | None) -> str:
    """Collapse all whitespace runs to single spaces."""
    return _WS.sub(" ", text or "").strip()


def clip(text: str | None, limit: int, ellipsis: str = "…") -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - len(ellipsis))].rstrip()
    return cut + ellipsis


def fold(text: str | None) -> str:
    """Lower-case and strip accents for forgiving comparisons."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).casefold().strip()


class _TextExtractor(HTMLParser):
    _BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote", "hr"}
    _SKIP = {"script", "style", "head", "title", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(markup: str | None) -> str:
    """Best-effort readable text from an HTML email body."""
    if not markup:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", markup)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [squash(line) for line in text.splitlines()]
    out: list[str] = []
    blank = False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    return "\n".join(out).strip()


def parse_hhmm(value: str, default: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", value or "")
    if not match:
        return default
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return default
    return hour, minute


def human_delta(seconds: float) -> str:
    """'in 5 min', '2 h ago' style relative time."""
    future = seconds >= 0
    seconds = abs(seconds)
    if seconds < 45:
        return "now"
    if seconds < 3600:
        amount, unit = round(seconds / 60), "min"
    elif seconds < 86400:
        amount, unit = round(seconds / 3600), "h"
    else:
        amount, unit = round(seconds / 86400), "d"
    return f"in {amount} {unit}" if future else f"{amount} {unit} ago"
