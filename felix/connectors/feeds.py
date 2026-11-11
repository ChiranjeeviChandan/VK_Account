"""News watchlist: RSS and Atom feeds the owner wants Felix to keep an eye on.

Headlines are scored like any other signal, so bad press about the company or
a competitor shows up in the pulse. An optional keyword list keeps only the
items that mention something the owner cares about.
"""

from __future__ import annotations

import email.utils
import logging
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..util import clip, fold, html_to_text, squash

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix

log = logging.getLogger("felix.feeds")


def parse_feed(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Return (feed title, items) from RSS 2.0 or Atom XML. Never raises."""
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError:
        return "", []

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    def child_text(node: ET.Element, *names: str) -> str:
        for child in node:
            if local(child.tag) in names and (child.text or "").strip():
                return child.text.strip()
        return ""

    title = ""
    items: list[dict[str, Any]] = []
    for node in root.iter():
        kind = local(node.tag)
        if kind in ("channel", "feed") and not title:
            title = child_text(node, "title")
        if kind not in ("item", "entry"):
            continue
        link = child_text(node, "link")
        if not link:
            for child in node:
                if local(child.tag) == "link" and child.get("href"):
                    link = child.get("href", "")
                    break
        published = child_text(node, "pubdate", "published", "updated", "date")
        items.append({
            "guid": child_text(node, "guid", "id") or link or child_text(node, "title"),
            "title": squash(html_to_text(child_text(node, "title"))),
            "link": link,
            "summary": clip(squash(html_to_text(child_text(node, "description", "summary", "content", "encoded"))), 500),
            "ts": _timestamp(published),
        })
    return title, [i for i in items if i["title"]]


def _timestamp(value: str) -> float:
    if not value:
        return 0.0
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


class Feeds:
    def __init__(self, app: "Felix") -> None:
        self.app = app

    def sync(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"new": 0, "errors": []}
        feeds = self.app.config.get("pulse.feeds", []) or []
        if not feeds or not self.app.net.online:
            return summary
        watch = [fold(w) for w in self.app.config.get("pulse.watch", []) or [] if w]
        for url in feeds:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "felix-assistant", "Accept": "*/*"})
                with urllib.request.urlopen(request, timeout=20) as response:
                    text = response.read(2_000_000).decode("utf-8", "replace")
            except (urllib.error.URLError, OSError, ValueError) as exc:
                summary["errors"].append(f"{url}: {getattr(exc, 'reason', exc)}")
                continue
            title, items = parse_feed(text)
            for item in items[:60]:
                haystack = fold(f"{item['title']} {item['summary']}")
                if watch and not any(word in haystack for word in watch):
                    continue
                ts = item["ts"] or self.app.clock.ts()
                row_id = self.app.store.insert("feed_items", {
                    "feed": url, "guid": item["guid"][:500], "title": clip(item["title"], 300), "link": item["link"],
                    "summary": item["summary"], "ts": ts}, ignore=True)
                if not row_id:
                    continue
                summary["new"] += 1
                self.app.pulse.ingest("feed", row_id, ts, item["summary"], title=item["title"], who=title or url,
                                      source=title or "news", topic=f"feed:{row_id}")
        return summary
