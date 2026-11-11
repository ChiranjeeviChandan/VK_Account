"""The pulse: what is happening around the owner, and what is broken.

Every inbound email, chat message, alert and watched headline becomes a
*signal* scored by the offline analyser. This module groups signals into
issues, ranks them, notices when one has been resolved, and tracks whose tone
is getting worse.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from ..tools import READ, Registry, ToolContext
from ..util import clip, squash
from .actions import extract_actions
from .sentiment import analyze, clean_subject, keywords, normalize_subject

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix

BROKEN_THRESHOLD = 0.42
BROKEN_CATEGORIES = ("incident", "security", "legal", "churn", "complaint", "blocker", "payment", "people", "alert")
CATEGORY_LABEL = {
    "incident": "outage / failure", "security": "security", "legal": "legal", "churn": "customer at risk",
    "complaint": "complaint", "blocker": "blocked / delayed", "payment": "payment", "people": "people",
    "alert": "system alert",
}


class Pulse:
    def __init__(self, app: "Felix") -> None:
        self.app = app

    # -- ingest ------------------------------------------------------------------
    def ingest(self, kind: str, ref_id: int, ts: float, text: str, title: str = "", who: str = "",
               who_addr: str = "", source: str = "", direction: str = "in", automated: bool = False,
               answered: bool = False, topic: str | None = None) -> dict[str, Any] | None:
        """Score one item and store it as a signal. Outbound items are not
        scored, but they clear 'needs reply' on what they answer."""
        if direction != "in":
            return None
        analysis = analyze(text, title=title, sender=who_addr or who, automated=automated)
        vip = self.app.contacts.is_vip(who_addr, who)
        severity = analysis.severity
        if vip and severity > 0.15:
            severity = min(1.0, severity * 1.25 + 0.05)
        categories = list(analysis.categories)
        if analysis.bulk:
            categories.append("bulk")
        needs_reply = (
            not analysis.bulk and not answered and kind in ("email", "message")
            and (analysis.asks or (analysis.sentiment <= -0.35 and severity >= 0.3))
        )
        if topic is None:
            topic = f"mail:{normalize_subject(title)}" if kind == "email" and title else f"{kind}:{ref_id}"
        snippet = clip(squash(text), 220)
        values = {
            "kind": kind, "ref_id": ref_id, "ts": ts, "source": source, "who": who, "who_addr": who_addr,
            "title": clip(squash(title), 160), "snippet": snippet,
            "sentiment": analysis.sentiment, "urgency": analysis.urgency, "severity": round(severity, 3),
            "categories": categories, "topic": topic, "needs_reply": int(needs_reply), "vip": int(vip),
            "engine": "lexicon",
        }
        existing = self.app.store.one("SELECT id, resolved FROM signals WHERE kind = ? AND ref_id = ?", (kind, ref_id))
        if existing:
            self.app.store.update("signals", values, "id = ?", (existing["id"],))
            signal_id = existing["id"]
        else:
            signal_id = self.app.store.insert("signals", values)
            if needs_reply and self.app.config.get("pulse.suggest_tasks", True):
                self._suggest_tasks(kind, ref_id, text, who or who_addr)
        values["id"] = signal_id
        values["why"] = analysis.signals
        if severity >= BROKEN_THRESHOLD and not analysis.bulk:
            self.app.bus.publish("pulse.flagged", {
                "id": signal_id, "kind": kind, "ref_id": ref_id, "title": values["title"] or snippet,
                "who": who, "severity": values["severity"], "categories": categories,
            })
        return values

    def _suggest_tasks(self, kind: str, ref_id: int, text: str, who: str) -> None:
        try:
            actions = extract_actions(text, self.app.clock.now(), self.app.config.get("owner.date_order", "dmy"))
        except Exception:
            actions = []
        for action in actions:
            title = f"{action.title} (asked by {who})" if who else action.title
            self.app.tasks.add(clip(title, 140), due=action.due, origin=kind, origin_ref=str(ref_id),
                               notes=clip(action.sentence, 300), suggested=True)

    def mark_answered(self, kind: str, ref_ids: list[int]) -> None:
        for ref_id in ref_ids:
            self.app.store.update("signals", {"needs_reply": 0}, "kind = ? AND ref_id = ?", (kind, ref_id))

    def mark_topic_answered(self, topic: str, before_ts: float) -> None:
        self.app.store.update("signals", {"needs_reply": 0}, "topic = ? AND ts <= ?", (topic, before_ts))

    def remove(self, kind: str, ref_id: int) -> None:
        self.app.store.delete("signals", "kind = ? AND ref_id = ?", (kind, ref_id))

    def dismiss(self, signal_id: int) -> bool:
        """The owner says this one is handled; hide it from 'broken'."""
        return bool(self.app.store.update("signals", {"resolved": 1, "needs_reply": 0}, "id = ?", (signal_id,)))

    # -- report --------------------------------------------------------------------
    def report(self, hours: float | None = None) -> dict[str, Any]:
        hours = float(hours or self.app.config.get("pulse.window_hours", 24))
        now = self.app.clock.ts()
        start = now - hours * 3600
        rows = self.app.store.query("SELECT * FROM signals WHERE ts >= ? AND ts <= ? ORDER BY ts", (start, now + 60))
        previous = self.app.store.query(
            "SELECT sentiment, categories FROM signals WHERE ts >= ? AND ts < ?", (start - hours * 3600, start))
        people_rows = [r for r in rows if "bulk" not in r["categories"]]

        clusters = _merge_related(_cluster(people_rows))
        broken: list[dict[str, Any]] = []
        recovered: list[dict[str, Any]] = []
        happening: list[dict[str, Any]] = []
        for cluster in clusters:
            summary = self._summarise(cluster, now)
            if summary["was_broken"] and summary["resolved"]:
                recovered.append(summary)
            elif summary["was_broken"]:
                broken.append(summary)
            elif not all("win" in r["categories"] for r in cluster):
                happening.append(summary)      # pure good news is listed once, under wins
        broken.sort(key=lambda c: -c["score"])
        recovered.sort(key=lambda c: -c["last_ts"])
        happening.sort(key=lambda c: (-(c["count"] + (2 if c["vip"] else 0)), -c["last_ts"]))

        waiting = self.waiting_on_owner()
        wins = [
            {"who": r["who"] or r["who_addr"], "title": r["title"] or r["snippet"], "kind": r["kind"],
             "ref_id": r["ref_id"], "ts": r["ts"]}
            for r in people_rows if "win" in r["categories"] and r["sentiment"] > 0.3
        ]
        wins.sort(key=lambda w: -w["ts"])

        sentiments = [r["sentiment"] for r in people_rows if r["kind"] in ("email", "message")]
        earlier = [r["sentiment"] for r in previous if "bulk" not in r["categories"]]
        mood = _mean(sentiments)
        trend = round(mood - _mean(earlier), 3) if earlier and sentiments else 0.0

        return {
            "window_hours": hours,
            "generated_at": now,
            "counts": {
                "signals": len(people_rows),
                "emails": sum(1 for r in people_rows if r["kind"] == "email"),
                "messages": sum(1 for r in people_rows if r["kind"] == "message"),
                "negative": sum(1 for r in people_rows if r["sentiment"] <= -0.35),
                "positive": sum(1 for r in people_rows if r["sentiment"] >= 0.35),
                "needs_reply": len(waiting),
                "filtered_bulk": len(rows) - len(people_rows),
            },
            "mood": {"score": round(mood, 3), "label": _mood_label(mood, len(broken)), "trend": trend},
            "broken": [_public(c) for c in broken[:10]],
            "recovered": [_public(c) for c in recovered[:5]],
            "happening": [_public(c) for c in happening[:10]],
            "wins": wins[:5],
            "waiting_on_you": waiting[:15],
            "people": self._people(people_rows),
            "news": self._news(rows),
        }

    def waiting_on_owner(self, days: float = 7) -> list[dict[str, Any]]:
        """Inbound items that still want an answer, most pressing first."""
        since = self.app.clock.ts() - days * 86400
        rows = self.app.store.query(
            "SELECT * FROM signals WHERE needs_reply = 1 AND resolved = 0 AND ts >= ? "
            "ORDER BY vip DESC, (urgency + severity) DESC, ts DESC LIMIT 60", (since,))
        now = self.app.clock.ts()
        return [{
            "kind": r["kind"], "ref_id": r["ref_id"], "who": r["who"] or r["who_addr"],
            "title": r["title"] or r["snippet"], "snippet": r["snippet"], "ts": r["ts"],
            "age_hours": round((now - r["ts"]) / 3600, 1), "vip": bool(r["vip"]),
            "urgent": r["urgency"] >= 0.5, "source": r["source"],
        } for r in rows]

    def _summarise(self, cluster: list[dict[str, Any]], now: float) -> dict[str, Any]:
        cluster = sorted(cluster, key=lambda r: r["ts"])
        worst = max(cluster, key=lambda r: r["severity"])
        latest = cluster[-1]
        categories: list[str] = []
        for row in cluster:
            for name in row["categories"]:
                if name not in categories:
                    categories.append(name)
        broken_cats = [c for c in categories if c in BROKEN_CATEGORIES]
        was_broken = worst["severity"] >= BROKEN_THRESHOLD and bool(broken_cats) and not all(r["resolved"] for r in cluster)
        resolved = any(
            "resolved" in r["categories"] and r["ts"] >= worst["ts"] and r is not worst for r in cluster
        ) or ("resolved" in latest["categories"] and latest["sentiment"] >= 0)
        if any(r["resolved"] for r in cluster if r["severity"] >= BROKEN_THRESHOLD):
            resolved = True
        age_hours = max(0.0, (now - latest["ts"]) / 3600)
        decay = 0.5 ** (age_hours / 36)          # an issue nobody mentions fades over a day or two
        vip = any(r["vip"] for r in cluster)
        waiting = any(r["needs_reply"] for r in cluster)
        score = worst["severity"] * (0.55 + 0.45 * decay) * (1 + 0.12 * math.log2(len(cluster))) + (0.06 if waiting else 0)
        people = []
        for row in cluster:
            name = row["who"] or row["who_addr"]
            if name and name not in people:
                people.append(name)
        title = next((r["title"] for r in reversed(cluster) if r["title"]), "") or clip(worst["snippet"], 90)
        return {
            "title": _clean_title(title), "score": round(min(score, 1.5), 3), "severity": worst["severity"],
            "categories": broken_cats or [c for c in categories if c not in ("ask", "bulk")],
            "count": len(cluster), "people": people[:5], "vip": vip, "needs_reply": waiting,
            "first_ts": cluster[0]["ts"], "last_ts": latest["ts"],
            "sentiment": round(_mean([r["sentiment"] for r in cluster]), 3),
            "latest": latest["snippet"], "worst": worst["snippet"],
            "was_broken": was_broken, "resolved": resolved,
            "items": [{"kind": r["kind"], "ref_id": r["ref_id"], "signal_id": r["id"]} for r in cluster[-6:]],
            "sources": sorted({r["source"] or r["kind"] for r in cluster}),
        }

    def _people(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Whose messages lean negative, and whose tone is sliding."""
        by_person: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["kind"] not in ("email", "message"):
                continue
            key = row["who"] or row["who_addr"]
            if key:
                by_person.setdefault(key, []).append(row)
        out = []
        for name, items in by_person.items():
            average = _mean([r["sentiment"] for r in items])
            entry: dict[str, Any] = {"who": name, "count": len(items), "sentiment": round(average, 2),
                                     "vip": any(r["vip"] for r in items)}
            if len(items) >= 3:
                half = len(items) // 2
                slide = _mean([r["sentiment"] for r in items[half:]]) - _mean([r["sentiment"] for r in items[:half]])
                if slide <= -0.35:
                    entry["tone"] = "worsening"
                elif slide >= 0.35:
                    entry["tone"] = "improving"
            if average <= -0.3 or entry.get("tone") or entry["vip"] or len(items) >= 3:
                out.append(entry)
        out.sort(key=lambda e: (e["sentiment"], -e["count"]))
        return out[:8]

    def _news(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        items = [r for r in rows if r["kind"] == "feed"]
        items.sort(key=lambda r: (-(abs(r["sentiment"]) + r["severity"]), -r["ts"]))
        return [{
            "title": r["title"], "source": r["source"], "sentiment": r["sentiment"], "ts": r["ts"],
            "tone": "negative" if r["sentiment"] <= -0.35 else "positive" if r["sentiment"] >= 0.35 else "neutral",
        } for r in items[:8]]

    def explain(self, kind: str, ref_id: int) -> dict[str, Any] | None:
        """Why did Felix flag this? Re-runs the analyser and shows its evidence."""
        row = self.app.store.one("SELECT * FROM signals WHERE kind = ? AND ref_id = ?", (kind, ref_id))
        if not row:
            return None
        analysis = analyze(row["snippet"], title=row["title"], sender=row["who_addr"] or row["who"])
        return {"title": row["title"], "who": row["who"], "sentiment": row["sentiment"], "urgency": row["urgency"],
                "severity": row["severity"], "categories": row["categories"], "evidence": analysis.signals}

    # -- text for humans -------------------------------------------------------------
    def render(self, report: dict[str, Any], limit: int = 5) -> str:
        """Plain-text summary used offline and as the fallback in briefs."""
        lines: list[str] = []
        counts = report["counts"]
        hours = int(report["window_hours"])
        lines.append(
            f"Last {hours}h: {counts['emails']} emails, {counts['messages']} messages; "
            f"mood {report['mood']['label']}"
            + (f" ({_trend_word(report['mood']['trend'])})" if abs(report["mood"]["trend"]) >= 0.15 else "") + "."
        )
        if report["broken"]:
            lines.append("")
            lines.append("What's broken:")
            for item in report["broken"][:limit]:
                lines.append("- " + self.render_issue(item))
        else:
            lines.append("")
            lines.append("Nothing looks broken.")
        if report["recovered"]:
            lines.append("")
            lines.append("Recovered:")
            for item in report["recovered"][:3]:
                lines.append(f"- {item['title']} (resolved)")
        shown = {(i["kind"], i["ref_id"]) for b in report["broken"][:limit] for i in b["items"]}
        waiting = [w for w in report["waiting_on_you"] if (w["kind"], w["ref_id"]) not in shown]
        if waiting:
            lines.append("")
            lines.append(f"Also waiting on you ({len(waiting)}):" if shown else f"Waiting on you ({len(waiting)}):")
            for item in waiting[:limit]:
                flag = " [VIP]" if item["vip"] else ""
                urgent = " - urgent" if item["urgent"] else ""
                lines.append(f"- {item['who']}{flag}: {clip(item['title'], 80)}{urgent}")
        if report["happening"]:
            lines.append("")
            lines.append("What's happening:")
            for item in report["happening"][:limit]:
                who = ", ".join(item["people"][:2])
                extra = f" ({item['count']} messages)" if item["count"] > 1 else ""
                lines.append(f"- {clip(item['title'], 80)}" + (f" - {who}" if who else "") + extra)
        if report["wins"]:
            lines.append("")
            lines.append("Good news:")
            for item in report["wins"][:3]:
                lines.append(f"- {clip(item['title'], 80)}" + (f" - {item['who']}" if item["who"] else ""))
        tense = [p for p in report["people"] if p.get("tone") == "worsening"]
        if tense:
            lines.append("")
            lines.append("Tone getting worse: " + ", ".join(p["who"] for p in tense[:3]))
        return "\n".join(lines)

    def render_issue(self, item: dict[str, Any]) -> str:
        label = ", ".join(CATEGORY_LABEL.get(c, c) for c in item["categories"][:2]) or "issue"
        who = ", ".join(item["people"][:2])
        parts = [f"{clip(item['title'], 80)} [{label}]"]
        if who:
            parts.append(f"from {who}" + (" (VIP)" if item["vip"] else ""))
        if item["count"] > 1:
            parts.append(f"{item['count']} messages")
        if item["needs_reply"]:
            parts.append("awaiting your reply")
        return " - ".join(parts)

    # -- tools -------------------------------------------------------------------------
    def register(self, tools: Registry) -> None:
        @tools.tool(
            "pulse_report",
            "Summarise what is happening around the owner and what is broken, from the sentiment of recent "
            "emails, chat messages, alerts and watched news. Returns ranked issues (outages, complaints, "
            "customers at risk, legal, payments, people), resolved issues, items waiting on the owner, wins, "
            "and whose tone is worsening. Call this for 'what's happening', 'what's broken', 'anything on fire', "
            "'how is the mood', or before writing any status summary.",
            {"hours": {"type": "number", "minimum": 1, "maximum": 720, "description": "Look-back window; default 24"}},
            risk=READ, group="pulse",
        )
        def pulse_report(args: dict[str, Any], ctx: ToolContext) -> Any:
            report = self.report(args.get("hours"))
            report["summary_text"] = self.render(report)
            return report

        @tools.tool(
            "pulse_explain",
            "Explain why a specific email or message was flagged: the phrases that drove its sentiment, "
            "urgency and category. Use when the owner asks 'why is this marked broken?'.",
            {"kind": {"type": "string", "enum": ["email", "message", "feed", "notification"]},
             "id": {"type": "integer", "description": "The item's id"}},
            ["kind", "id"], risk=READ, group="pulse",
        )
        def pulse_explain(args: dict[str, Any], ctx: ToolContext) -> Any:
            found = self.explain(args["kind"], args["id"])
            return found or {"message": "That item has not been analysed."}


# -- grouping helpers ----------------------------------------------------------------

def _cluster(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["kind"] == "feed":
            continue
        by_topic.setdefault(row["topic"] or f"{row['kind']}:{row['ref_id']}", []).append(row)
    return list(by_topic.values())


def _merge_related(clusters: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]]:
    """Join clusters that are clearly about the same thing across channels:
    an email thread 'Payment gateway down' and a WhatsApp 'payment gateway is
    not working' become one issue."""
    keyed = []
    for cluster in clusters:
        text = " ".join(f"{r['title']} {r['title']} {r['snippet']}" for r in cluster)
        keyed.append((set(keywords(text, limit=7)), cluster))
    merged: list[tuple[set[str], list[dict[str, Any]]]] = []
    for words, cluster in keyed:
        target = None
        hot = max(r["severity"] for r in cluster) >= 0.3
        for index, (other_words, other) in enumerate(merged):
            shared = words & other_words
            if len(shared) < 2:
                continue
            # Two reports of one problem rarely use all the same words, so compare
            # against the smaller description rather than the union.
            overlap = len(shared) / max(1, min(len(words), len(other_words)))
            if overlap >= 0.25 and (hot or max(r["severity"] for r in other) >= 0.3):
                target = index
                break
        if target is None:
            merged.append((set(words), list(cluster)))
        else:
            merged[target] = (merged[target][0] | words, merged[target][1] + cluster)
    return [cluster for _, cluster in merged]


def _public(summary: dict[str, Any]) -> dict[str, Any]:
    keep = ("title", "score", "severity", "categories", "count", "people", "vip", "needs_reply",
            "first_ts", "last_ts", "sentiment", "latest", "items", "sources", "resolved")
    return {k: summary[k] for k in keep}


def _clean_title(title: str) -> str:
    cleaned = clean_subject(title)
    return (cleaned[:1].upper() + cleaned[1:]) if cleaned else title


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _mood_label(mood: float, broken: int) -> str:
    if broken >= 3 or mood <= -0.3:
        return "tense"
    if broken >= 1 or mood <= -0.1:
        return "mixed"
    if mood >= 0.25:
        return "upbeat"
    return "calm"


def _trend_word(trend: float) -> str:
    return "improving" if trend > 0 else "worsening"
