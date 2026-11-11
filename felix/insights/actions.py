"""Find the things other people are asking the owner to do (offline heuristics)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from ..timeparse import parse_when
from ..util import clip, squash
from .sentiment import strip_quoted

_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_GREETING = re.compile(r"^(?:hi|hello|hey|dear|good\s+(?:morning|afternoon|evening))\b[^,\n]{0,30}[,!]?\s*", re.I)

_VERBS = (r"send|share|confirm|review|approve|advise|check|call|reply|respond|let|provide|arrange|sign|look|update|"
          r"forward|revert|schedule|book|pay|clear|release|join|prepare|complete|submit|fix|resolve|get|help|make|do")
_NOUNS = (r"approval|go[\s-]?ahead|sign[\s-]?off|decision|input|confirmation|feedback|reply|response|answer|signature|"
          r"review|comments?|thoughts|nod|okay|ok")

# (pattern, title prefix). Each pattern captures the action that follows the polite wrapper.
_ASKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:can|could|would|will)\s+you\s+(?:please\s+|kindly\s+)?(?P<act>[a-z].{3,160})", re.I), ""),
    (re.compile(rf"\b(?:please|pls|plz|kindly)\s+(?P<act>(?:{_VERBS})\b.{{0,160}})", re.I), ""),
    (re.compile(r"\b(?:need|want|require|request(?:ing)?)\s+(?:you|your\s+help)\s+to\s+(?P<act>[a-z].{3,160})", re.I), ""),
    (re.compile(rf"\b(?:i|we)\s+need\s+(?:your\s+)?(?P<act>(?:{_NOUNS})\b.{{0,120}})", re.I), "Give your "),
    (re.compile(rf"\bwaiting\s+(?:on|for)\s+your\s+(?P<act>(?:{_NOUNS})\b.{{0,120}})", re.I), "Give your "),
    # A plain instruction: "Call me when free", "Send me the deck by Friday".
    (re.compile(r"^(?P<act>(?:call|ring|phone|send|share|confirm|review|approve|check|reply|sign|forward|schedule|book|"
                r"pay|join|prepare|submit|fix|ping|email|text|message|remind)\s+"
                r"(?:me|us|the|this|that|these|those|your|back|it|a|an|with|to)\b.{0,160})", re.I), ""),
]
_LEADING_FILLER = re.compile(r"^(?:to\s+|just\s+|also\s+|quickly\s+|kindly\s+|please\s+)+", re.I)
_CONTEXT_TAIL = re.compile(r"[\s,;:–—-]+(?:and|so|but|now|still|just|we\s+are|we're|i\s+am|i'm)?[\s,;:–—-]*$", re.I)


@dataclass
class ActionItem:
    title: str
    due: datetime | None = None
    sentence: str = ""


def extract_actions(text: str, now: datetime, date_order: str = "dmy", limit: int = 2) -> list[ActionItem]:
    """Return up to ``limit`` requests found in a message addressed to the owner."""
    body = strip_quoted(text or "")
    items: list[ActionItem] = []
    seen: set[str] = set()
    for raw in _SENTENCE.split(body):
        sentence = _GREETING.sub("", squash(raw))
        if len(sentence) < 8 or len(sentence) > 400:
            continue
        for pattern, prefix in _ASKS:
            match = pattern.search(sentence)
            if not match:
                continue
            action = _LEADING_FILLER.sub("", match.group("act")).strip(" .?!,;:")
            when = parse_when(action, now, date_order=date_order)
            due = None
            if when is not None and (when.has_date or when.has_time):
                due = when.start if when.has_time else when.start.replace(hour=18, minute=0)
                rest = (when.rest or "").strip(" .?!,;:")
                # "before Thursday's call" names an event, not a deadline phrase: keep the wording whole.
                if rest and not re.search(r"(?:^|\s)['’]s\b", rest):
                    action = rest
            action = re.sub(r"\s+(?:by|before|on|at|until|till)$", "", action, flags=re.I).strip(" .?!,;:")
            if len(action) < 4:
                continue
            if prefix:
                # "... - waiting on your go-ahead": say what the go-ahead is for.
                context = _CONTEXT_TAIL.sub("", sentence[:match.start()]).strip(" .?!,;:")
                answer = re.match(r"(?:reply|response|answer)\b\s*", action, re.I)
                action = "Reply " + action[answer.end():] if answer else prefix + action[0].lower() + action[1:]
                action = action.strip()
                if len(context.split()) >= 3 and len(action.split()) <= 4:
                    action = f"{action}: {context[0].lower() + context[1:]}"
            title = clip(action[0].upper() + action[1:], 90)
            key = title.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(ActionItem(title=title, due=due, sentence=sentence))
            break
        if len(items) >= limit:
            break
    return items
