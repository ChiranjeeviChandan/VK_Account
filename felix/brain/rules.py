"""The offline command engine: plain commands, no language model, no network.

This is what keeps Felix useful on a plane. It understands a fixed set of
phrasings ("what's on tomorrow", "remind me to call the bank at 5",
"whatsapp Ravi saying I'm late", "lock the laptop") and turns each into the
same tool calls a model would make - so approvals and auditing apply equally.
It never guesses: text it does not recognise is handed back unanswered.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable

from ..insights.sentiment import keywords
from ..timeparse import format_when, parse_duration, parse_range, parse_when
from ..tools import ToolContext, ToolError, ToolResult
from ..util import clip, human_delta, plural

if TYPE_CHECKING:  # pragma: no cover
    from ..app import Felix

Handler = Callable[["Rules", re.Match, ToolContext], "str | None"]
_INTENTS: list[tuple[str, re.Pattern[str], Handler, bool, bool]] = []

_POLITE = re.compile(
    r"^(?:(?:hey|hi|hello|ok|okay)\s+)?(?:felix[,:]?\s+)?(?:(?:please|pls|kindly)\s+)?"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|i\s+(?:want|need|would like|'d like)\s+(?:you\s+)?to\s+|"
    r"i\s+want\s+)?", re.I)
_SAY = r"(?:saying|that says|to say|telling (?:him|her|them)|that|:| - )"
_SAY_ONE = r"(?:saying|that says|to say|telling (?:him|her|them)|that|:|,| - )"     # one recipient: a comma may introduce the text


def intent(pattern: str, name: str, fast: bool = False, verbatim: bool = False):
    """Register a handler.

    ``fast`` intents may run even when Claude is available, because the
    command is unambiguous and speed matters. ``verbatim`` intents carry text
    that will be sent or stored as the owner wrote it, so their closing
    punctuation is kept.
    """
    def wrap(fn: Handler) -> Handler:
        _INTENTS.append((name, re.compile(pattern, re.I | re.S), fn, fast, verbatim))
        return fn
    return wrap


def normalise(text: str) -> str:
    """Drop the polite wrapper ("hey Felix, could you please ..."), keep the rest untouched."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    return _POLITE.sub("", text, count=1).strip()


def _bare(text: str) -> str:
    """For commands that carry no message: closing punctuation and a trailing 'please' mean nothing."""
    text = text.rstrip(" ?.!")
    return re.sub(r"[\s,]+(?:please|pls)$", "", text, flags=re.I).rstrip(" ?.!,")


class Rules:
    name = "rules"

    def __init__(self, app: "Felix") -> None:
        self.app = app

    # -- entry points ------------------------------------------------------------
    def respond(self, text: str, ctx: ToolContext, fast_only: bool = False) -> str | None:
        """Answer ``text`` if it matches a known command, else None."""
        clean = normalise(text)
        bare = _bare(clean)
        if not bare:
            return None
        for _name, pattern, handler, fast, verbatim in _INTENTS:
            if fast_only and not fast:
                continue
            match = pattern.match(clean if verbatim else bare)
            if not match:
                continue
            try:
                answer = handler(self, match, ctx)
            except ToolError as exc:
                return str(exc)
            if answer is not None:
                return answer
        return None

    def unknown(self, text: str, ctx: ToolContext) -> str:
        """What to say when nothing matched and no model is reachable."""
        hits = self.app.store.search(" ".join(keywords(text, limit=5)), limit=5, match_any=True)
        lines = ["I'm working without a language model right now, so I only understand direct commands."]
        if hits:
            lines.append("")
            lines.append("Here is what I have stored that mentions it:")
            for hit in hits:
                lines.append(f"• [{hit['kind']} #{hit['ref']}] {clip(hit['title'] or hit['excerpt'], 90)}")
        lines.append("")
        lines.append('Try: "what\'s on today", "my tasks", "unread emails", "whatsapp", "what\'s broken", '
                     '"remind me to ... at 5pm", or say "help".')
        return "\n".join(lines)

    # -- helpers -----------------------------------------------------------------
    def call(self, ctx: ToolContext, tool: str, **args: Any) -> ToolResult:
        return self.app.tools.execute(tool, {k: v for k, v in args.items() if v is not None}, ctx)

    def say(self, result: ToolResult, fmt: Callable[[Any], str] | None = None) -> str:
        if result.pending:
            return (f"Ready - waiting for your go-ahead (#{result.pending['id']}):\n{result.pending['preview']}\n\n"
                    'Reply "yes" to go ahead or "no" to cancel.')
        if not result.ok:
            return result.error or "That did not work."
        return fmt(result.data) if fmt else result.for_human()

    def order(self) -> str:
        return self.app.config.get("owner.date_order", "dmy")

    def split_name(self, rest: str) -> tuple[str, str]:
        """'Ravi Kumar I am late' -> ('Ravi Kumar', 'I am late'), using known chats and contacts."""
        words = rest.split()
        best = 1
        for size in range(min(4, len(words) - 1), 0, -1):
            name = " ".join(words[:size])
            try:
                known = self.app.whatsapp.find_chat(name) is not None
            except ToolError:
                known = True
            if not known:
                matches = self.app.contacts.resolve(name)
                known = bool(matches) and matches[0].get("score", 0) >= 0.82 and matches[0].get("id", 0) != 0
            if known:
                best = size
                break
        message = re.sub(r"^(?:that|to say|saying)\s+", "", " ".join(words[best:]), flags=re.I)
        return " ".join(words[:best]), message


# -- formatting ------------------------------------------------------------------

def _events(data: dict[str, Any]) -> str:
    events = data["events"]
    if not events:
        return f"Nothing on your calendar {_for(data['period'])}."
    lines = [f"{data['period'].capitalize()} - {len(events)} event{'s' if len(events) != 1 else ''}:"]
    for event in events:
        where = f" ({event['location']})" if event.get("location") else ""
        lines.append(f"• {event['when']}  {event['title']}{where}")
    if data.get("overlapping"):
        lines.append("Clash: " + " / ".join(data["overlapping"]) + " overlap.")
    return "\n".join(lines)


def _for(period: str) -> str:
    return period if period in ("today", "tomorrow", "yesterday") else f"for {period}"


def _emails(data: dict[str, Any]) -> str:
    emails = data["emails"]
    if not emails:
        return data.get("note") or "No emails match."
    lines = [f"{len(emails)} email{'s' if len(emails) != 1 else ''} ({data['unread_total']} unread in total):"]
    for mail in emails:
        flags = "".join(f" [{f}]" for f, on in (("unread", mail.get("unread")), ("needs reply", mail.get("needs_reply")),
                                               ("VIP", mail.get("vip")), ("urgent", mail.get("urgent"))) if on)
        lines.append(f"• #{mail['id']} {mail['from']} - {clip(mail['subject'], 60)}{flags}")
    lines.append('Say "read email <number>" to open one.')
    if data.get("note"):
        lines.append(data["note"])
    return "\n".join(lines)


def _chats(data: dict[str, Any]) -> str:
    chats = data["chats"]
    if not chats:
        return data.get("note") or "No messages."
    lines = [f"{data['unread_total']} unread message{'s' if data['unread_total'] != 1 else ''}:"
             if data["unread_total"] else "No unread messages. Recent chats:"]
    for chat in chats:
        unread = f" ({chat['unread']} new)" if chat.get("unread") else ""
        flag = " [needs reply]" if chat.get("needs_reply") else ""
        last = f": {clip(chat.get('last_message', ''), 70)}" if chat.get("last_message") else ""
        lines.append(f"• {chat['chat']}{unread}{flag}{last}")
    return "\n".join(lines)


# =================================================================================
# Meta
# =================================================================================

HELP = """I'm Felix. Online I understand anything you ask; offline I follow direct commands like these:

Day        brief me · evening summary · what's broken · what's happening · who is waiting on me
Calendar   what's on tomorrow · next meeting · am I free Friday afternoon · schedule call with Ravi tomorrow 3pm for 30 min · move board review to Friday 10am · cancel dentist
To-do      my tasks · add task send the deck by Friday · remind me to call the bank at 5pm · done send the deck
Email      unread emails · emails from Ravi · read email 12 · reply to 12 saying Approved · email Meera saying The deck is attached
WhatsApp   whatsapp · read messages from Ravi · whatsapp Ravi saying Running 10 minutes late · text Meera saying Call me
Phone      call Ravi · find my phone · flashlight on · set alarm for 6:30am · timer 10 minutes · phone battery · silence my phone
Laptop     open Excel · volume 40 · mute · lock · screenshot · find file budget · run: git status
Memory     remember that the board meets on the first Monday · what do you know about the board · search lease
Felix      status · pending · go offline · go online · auto mode

Anything that sends a message or changes a file waits for your "yes"."""


@intent(r"^(?:help|commands|what can you do|what do you do|how do i use you|examples)$", "help", fast=True)
def _help(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return HELP


@intent(r"^(?:(?:go|switch to|work|stay|be|turn)\s+)?(offline|online|auto(?:matic)?)(?:\s+mode)?$|"
        r"^(disconnect|airplane mode|flight mode|reconnect|use (?:the )?internet(?: again)?)$", "mode", fast=True)
def _mode(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    word = (m.group(1) or m.group(2)).lower()
    mode = "offline" if word in ("offline", "disconnect", "airplane mode", "flight mode") else \
        "online" if word == "online" else "auto"
    rules.app.set_mode(mode)
    if mode == "offline":
        return ("Offline mode: I won't touch the network until you say \"go online\" or \"auto mode\". "
                "Calendar, tasks, cached mail and messages, and laptop control keep working; anything you send is queued.")
    if mode == "online":
        return "Online mode: I'll assume a connection and use it."
    state = "online" if rules.app.net.online else "offline right now"
    return f"Automatic mode: I'll use the internet whenever it is there ({state})."


@intent(r"^(?:status|felix status|system status|are you (?:online|there|working|connected)|how are you)$", "status", fast=True)
def _status(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    app = rules.app
    net = app.net.describe()
    brain = getattr(app, "brain", None)
    engine = brain.engine_name() if brain else "rules"
    engine_text = {"claude": "Claude (online)", "local": "local model (offline)",
                   "rules": "command engine (no language model)"}[engine]
    phone = app.phone.status()
    lines = [
        f"Connection: {'online' if net['online'] else 'offline'} (mode {net['mode']}).",
        f"Brain: {engine_text}.",
        "Phone: " + ("connected." if phone["connected"] else "paired, not reachable." if phone["paired"] else "not paired."),
        f"Mail accounts: {len(app.mail.accounts())}. Unread: {app.mail.unread_count()}.",
    ]
    pending, queued = len(app.policy.pending()), len(app.outbox.pending())
    if pending:
        lines.append(f"{plural(pending, 'action')} waiting for your approval - say \"pending\".")
    if queued:
        lines.append(f"{plural(queued, 'message')} queued to send.")
    return "\n".join(lines)


@intent(r"^(?:pending|approvals?|show approvals|what(?:'s| is) (?:pending|waiting)(?: for (?:my )?approval)?)$",
        "pending", fast=True)
def _pending(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    waiting = rules.app.policy.pending()
    if not waiting:
        return "Nothing is waiting for your approval."
    blocks = [f"#{row['id']}  {row['preview']}" for row in waiting]
    return f"{len(waiting)} waiting for your approval:\n\n" + "\n\n".join(blocks) + \
        '\n\nSay "approve <number>", "deny <number>" or "approve all".'


@intent(r"^(approve|deny|cancel|reject)\s+#?(\d+)$", "approve-by-number", fast=True)
def _decide(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    approval_id = int(m.group(2))
    if m.group(1).lower() == "approve":
        result = rules.app.policy.approve(approval_id, via=ctx.source)
    else:
        result = rules.app.policy.deny(approval_id, via=ctx.source)
    return result.for_human() if result.ok else (result.error or "That did not work.")


@intent(r"^approve (?:all|everything)$", "approve-all", fast=True)
def _approve_all(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    waiting = rules.app.policy.pending()
    if not waiting:
        return "Nothing is waiting for your approval."
    return "\n".join(rules.app.policy.approve(row["id"], via=ctx.source).for_human() for row in waiting)


@intent(r"^(?:new chat|new conversation|reset|start over|clear (?:the )?(?:chat|conversation)|"
        r"forget (?:this|the) conversation)$", "reset", fast=True)
def _reset(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    brain = getattr(rules.app, "brain", None)
    if brain is not None:
        brain.reset(ctx.thread)
    return "Started a fresh conversation. (Your notes, tasks and messages are untouched.)"


@intent(r"^what(?:'s| is)?\s+(?:the\s+)?(time|date|day)(?:\s+is it)?(?:\s+(?:now|today))?$|^what day is it$", "clock", fast=True)
def _clock(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    now = rules.app.clock.now()
    return now.strftime("It's %I:%M %p on %A %d %B %Y.").replace("It's 0", "It's ")


# =================================================================================
# Brief and pulse
# =================================================================================

@intent(r"^send (?:me )?(?:my |the |a )?(morning |evening |night )?brief(?:ing)?(?: (?:to|on|via) (?:my )?(?:whatsapp|phone))?$",
        "brief-send")
def _brief_send(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    word = (m.group(1) or "").strip().lower()
    kind = "evening" if word in ("evening", "night") else "morning" if word == "morning" else "now"
    return rules.say(rules.call(ctx, "brief_send", kind=kind), lambda d: d["message"])


@intent(r"^(?:give me (?:the |my |an? )?)?(?:evening|night|end of day|eod)\s+(?:brief(?:ing)?|summary|wrap[- ]?up|report)$|"
        r"^wrap up(?: my day)?$", "brief-evening")
def _brief_evening(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.app.briefing.render(rules.app.briefing.gather("evening"))


@intent(r"^(?:give me (?:a |my |the )?)?(morning |daily )?(?:brief(?:ing)?|brief me|my brief|summary|rundown|update)$|"
        r"^(?:summar(?:y|ise|ize) (?:of )?my day|how(?:'s| is| does) my day(?: look(?:ing)?)?|"
        r"what(?:'s| is| does) my day(?: look)? like|catch me up|what did i miss)$", "brief")
def _brief(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    kind = "morning" if (m.group(1) or "").strip().lower() == "morning" else "now"
    return rules.app.briefing.render(rules.app.briefing.gather(kind))


@intent(r"^(?:what(?:'s| is)(?: currently)? (?:broken|wrong|on fire|burning|not working|failing)|"
        r"any(?:thing)? (?:broken|wrong|on fire|urgent|burning)|any (?:issues|problems|fires|escalations|complaints|incidents)|"
        r"where are the fires)(?: (?:today|now|right now))?$", "broken")
def _broken(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    report = rules.app.pulse.report()
    if not report["broken"]:
        text = "Nothing looks broken in the last 24 hours."
        if report["recovered"]:
            text += " Resolved: " + "; ".join(r["title"] for r in report["recovered"][:3]) + "."
        return text
    lines = [f"{len(report['broken'])} thing{'s' if len(report['broken']) != 1 else ''} look broken:"]
    lines += ["• " + rules.app.pulse.render_issue(issue) for issue in report["broken"][:8]]
    if report["recovered"]:
        lines.append("Resolved: " + "; ".join(r["title"] for r in report["recovered"][:3]) + ".")
    return "\n".join(lines)


@intent(r"^(?:what(?:'s| is) (?:happening|going on|new|up)(?: around(?: me)?)?(?: (?:today|now))?|pulse|"
        r"anything (?:new|important|i should know)|what should i know|how(?:'s| is) the (?:mood|sentiment|tone)|sentiment)$",
        "pulse")
def _pulse(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.app.pulse.render(rules.app.pulse.report())


@intent(r"^(?:who(?:'s| is) waiting(?: (?:on|for) me)?|what(?:'s| is) waiting (?:on|for) me|waiting on me|"
        r"what needs (?:a |my )?(?:repl(?:y|ies)|attention|response)|who (?:needs|wants) (?:a |my )?repl(?:y|ies))$", "waiting")
def _waiting(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    waiting = rules.app.pulse.waiting_on_owner()
    if not waiting:
        return "Nobody is waiting on you."
    lines = [f"{len(waiting)} waiting on you:"]
    for item in waiting[:12]:
        flags = (" [VIP]" if item["vip"] else "") + (" [urgent]" if item["urgent"] else "")
        where = "email" if item["kind"] == "email" else item["source"]
        ref = f" (email #{item['ref_id']})" if item["kind"] == "email" else ""
        age = human_delta(-item["age_hours"] * 3600)
        lines.append(f"• {item['who']}{flags} via {where}, {age}: {clip(item['title'], 70)}{ref}")
    return "\n".join(lines)


# =================================================================================
# To-do
# =================================================================================

def _add_task(rules: Rules, ctx: ToolContext, rest: str, remind: bool) -> str:
    rest = rest.strip()
    when = parse_when(rest, rules.app.clock.now(), rules.order())
    title = (when.rest if when and when.rest else rest).strip()
    title = title[:1].upper() + title[1:]
    args: dict[str, Any] = {"title": title}
    if when is not None:
        moment = when.start if when.has_time else when.start.replace(hour=18)
        args["due"] = moment.isoformat()
        if remind:
            args["remind_at"] = (when.start if when.has_time else when.start.replace(hour=9)).isoformat()
    result = rules.call(ctx, "task_add", **args)

    def fmt(data: dict[str, Any]) -> str:
        note = ""
        if remind and "remind_at" in args:
            note = " I'll remind you " + format_when(when.start if when.has_time else when.start.replace(hour=9),
                                                      None, rules.app.clock.now()) + "."
        elif remind:
            note = " (No time given, so no reminder is set.)"
        return data["message"] + "." + note
    return rules.say(result, fmt)


@intent(r"^(?:add|new|create|make)\s+(?:a\s+|an\s+)?(?:task|to-?do|todo|action item)\s*(?:to\s+|:\s*|-\s*)?(.+)$|"
        r"^(?:todo|to-?do|task)\s*[:\-]\s*(.+)$", "task-add")
def _task_add(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return _add_task(rules, ctx, m.group(1) or m.group(2), remind=False)


@intent(r"^(?:remind me|set (?:a |an )?reminder)\s+(?:to\s+|that\s+|about\s+|for\s+)?(.+)$|"
        r"^(?:remember|don'?t (?:let me )?forget)\s+to\s+(.+)$", "remind")
def _remind(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return _add_task(rules, ctx, m.group(1) or m.group(2), remind=True)


@intent(r"^(?:(?:show|list|what are|what(?:'s| is))\s+)?(?:me\s+)?(?:my\s+|the\s+)?(?:tasks?|to-?dos?|to-?do list|todo list|"
        r"action items)(?:\s+(?:list|today|for today))?$|^what(?:'s| is) on my (?:plate|list|to-?do list)(?: today)?$|"
        r"^(?:show |open )?my list$|^what(?:'s| is) (?:left|pending)(?: to do)?(?: today)?$|"
        r"^what do i (?:need|have) to do(?: today)?$|^what(?:'s| is) (?:due|overdue)(?: today)?$", "task-list")
def _task_list(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    agenda = rules.app.tasks.agenda()
    if not any(agenda.values()):
        return "Your to-do list is empty."
    lines: list[str] = []
    for key, label in (("overdue", "Overdue"), ("today", "Due today"), ("upcoming", "Coming up"), ("someday", "No date")):
        if agenda[key]:
            lines.append(f"{label}:")
            lines += [f"• {rules.app.tasks.describe(task)}" for task in agenda[key][:10]]
    suggested = rules.app.tasks.suggestions()
    if suggested:
        lines.append(f"({len(suggested)} suggested from your messages - say \"suggestions\".)")
    return "\n".join(lines)


@intent(r"^(?:suggestions|suggested (?:tasks|to-?dos)|show suggestions)$", "task-suggestions")
def _task_suggestions(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    suggested = rules.app.tasks.suggestions()
    if not suggested:
        return "No suggested to-dos."
    lines = ["Requests I noticed in your messages:"]
    lines += [f"• {rules.app.tasks.describe(task)}" for task in suggested[:12]]
    lines.append('Say "accept <number>" to add one to your list, or "delete task <number>".')
    return "\n".join(lines)


@intent(r"^accept\s+(?:suggestion\s+|task\s+)?#?(\d+)$", "task-accept")
def _task_accept(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "task_update", task=m.group(1), accept=True), lambda d: d["message"] + ".")


@intent(r"^(?:done|finished|completed?|tick(?:ed)? off|check(?:ed)? off|close)\s*(?:with\s+|task\s+|to-?do\s+|:\s*)?#?(.+)$|"
        r"^mark(?:ed)?\s+(?:task\s+)?#?(.+?)\s+(?:as\s+)?(?:done|complete|completed|finished)$|"
        r"^mark(?:ed)?\s+(?:as\s+)?done\s*:?\s*#?(.+)$|^(?:task\s+)?#?(.+?)\s+is\s+done$", "task-done")
def _task_done(rules: Rules, m: re.Match, ctx: ToolContext) -> str | None:
    reference = next(g for g in m.groups() if g)
    result = rules.call(ctx, "task_complete", task=reference.strip())
    if not result.ok and "No open task matches" in (result.error or ""):
        return None if m.group(4) else result.error
    return rules.say(result, lambda d: d["message"] + ".")


@intent(r"^(?:delete|remove|drop|cancel)\s+(?:the\s+)?(?:task|to-?do|todo|reminder)\s+#?(.+)$", "task-delete")
def _task_delete(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "task_delete", task=m.group(1).strip()), lambda d: d["message"] + ".")


# =================================================================================
# Calendar
# =================================================================================

@intent(r"^(?:when\s+)?am i (?:free|available|busy)\s*(?P<a>.*)$|"
        r"^(?:when am i free|find(?: me)?(?: a| some)? (?:free )?(?:time|slots?)|any free (?:time|slots?)|free (?:time|slots?))\s*(?P<b>.*)$|"
        r"^find (?:me )?(?:a |an )?(?P<n>\d+|half an?|an?)[ -]?(?P<u>min(?:ute)?s?|hours?|hrs?)\s*(?:slot|window|gap|meeting)?\s*(?P<c>.*)$",
        "calendar-free")
def _free(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    rest = (m.group("a") or m.group("b") or m.group("c") or "").strip()
    args: dict[str, Any] = {"range": re.sub(r"^(?:on|for|in|at)\s+", "", rest) or "today"}
    if m.group("n"):
        amount = parse_duration(f"{m.group('n')} {m.group('u')}")
        if amount:
            args["duration_minutes"] = max(5, int(amount.total_seconds() // 60))
    else:
        found = re.search(r"(\d+)\s*(min(?:ute)?s?|hours?|hrs?)", rest, re.I)
        if found:
            amount = parse_duration(f"{found.group(1)} {found.group(2)}")
            if amount:
                args["duration_minutes"] = max(5, int(amount.total_seconds() // 60))
                args["range"] = (rest[:found.start()] + rest[found.end():]).strip() or "today"
    result = rules.call(ctx, "calendar_free", **args)

    def fmt(data: dict[str, Any]) -> str:
        if not data["free"]:
            return data["note"]
        return f"Free for {data['duration_minutes']} minutes or more:\n" + "\n".join(f"• {s['text']}" for s in data["free"])
    return rules.say(result, fmt)


@intent(r"^(?:what(?:'s| is)\s+)?(?:my\s+)?next(?:\s+(?:meeting|event|call|appointment))?$|^what(?:'s| is) next$|"
        r"^when(?:'s| is) my next (?:meeting|call|event)$", "calendar-next")
def _next(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    event = rules.app.calendar.next_event()
    if event is None:
        return "Nothing coming up in the next two weeks."
    delta = human_delta(event["start_ts"] - rules.app.clock.ts())
    where = f" at {event['location']}" if event["location"] else ""
    return f"Next: {event['title']}, {rules.app.calendar.when(event)}{where} ({delta})."


@intent(r"^(?:move|reschedule|shift|push|postpone|bring forward)\s+(?:my\s+|the\s+)?(.+?)\s+(?:to|until|till)\s+(.+)$",
        "calendar-move")
def _move(rules: Rules, m: re.Match, ctx: ToolContext) -> str | None:
    when = parse_when(m.group(2), rules.app.clock.now(), rules.order())
    if when is None:
        return None
    args: dict[str, Any] = {"event": m.group(1).strip(), "start": when.start.isoformat()}
    if when.end is not None:
        args["end"] = when.end.isoformat()
    return rules.say(rules.call(ctx, "calendar_update", **args), lambda d: d["message"])


@intent(r"^(?:schedule|book|set up|arrange|plan|block|put|add|create|new)\s+(?:a\s+|an\s+|me\s+)?"
        r"(?:(?P<noun>meeting|event|call|appointment|session|catch[- ]?up|sync|lunch|dinner|breakfast|slot|time|review)\b\s*)?"
        r"(?:(?:called|titled|named)\s+)?(?P<rest>.+)$", "calendar-create")
def _create(rules: Rules, m: re.Match, ctx: ToolContext) -> str | None:
    noun, rest = m.group("noun"), m.group("rest").strip()
    when = parse_when(rest, rules.app.clock.now(), rules.order())
    if when is None:
        if noun:
            return f"When should I put \"{rest}\" on the calendar? Say it again with a day and time."
        return None
    title = (when.rest or "").strip()
    title = re.sub(r"^(?:on|in)\s+(?:my\s+|the\s+)?calendar\s*", "", title, flags=re.I)
    if not noun and not title:
        return None          # "schedule for tomorrow" asks to see the schedule, not to create something
    label = (noun or "meeting").replace("catch up", "catch-up").capitalize()
    if not title:
        title = label
    elif re.match(r"^(?:with|for|about|to)\b", title, re.I) or noun in ("lunch", "dinner", "breakfast", "review"):
        title = f"{label} {title}"
    else:
        title = title[:1].upper() + title[1:]
    args: dict[str, Any] = {"title": title, "start": when.start.isoformat()}
    if when.end is not None:
        args["end"] = when.end.isoformat()
    if not when.has_time:
        args["all_day"] = True
        args["start"] = when.start.strftime("%Y-%m-%d")
    return rules.say(rules.call(ctx, "calendar_create", **args), lambda d: d["message"])


@intent(r"^(?:(?:what(?:'s| is|s)|show|list|check|tell me)\s+)?(?:me\s+)?(?:on\s+)?(?:my\s+|the\s+)?"
        r"(?:calendar|schedule|agenda|diary|meetings?|events?|appointments?)\s*(?:for|on|at|look like)?\s*(?P<a>.*)$|"
        r"^what(?:'s| is|s) on\s+(?P<b>.+)$|^(?:what do i have|what have i got|do i have (?:any(?:thing)?|meetings))\s*(?:on\s+)?(?P<c>.*)$",
        "calendar-list")
def _calendar_list(rules: Rules, m: re.Match, ctx: ToolContext) -> str | None:
    rest = (m.group("a") or m.group("b") or m.group("c") or "").strip()
    rest = re.sub(r"^(?:my\s+)?(?:calendar|schedule|agenda)\s*(?:for|on)?\s*", "", rest, flags=re.I)
    if not rest:
        return rules.say(rules.call(ctx, "calendar_list", range="today"), _events)
    now = rules.app.clock.now()
    if parse_range(rest, now, rules.order()) is not None:
        return rules.say(rules.call(ctx, "calendar_list", range=rest), _events)
    if m.group("b"):
        return None          # "what's on <something that is not a date>" is not a calendar question
    query = re.sub(r"^(?:with|about|for)\s+", "", rest, flags=re.I)
    result = rules.call(ctx, "calendar_list", start=now.isoformat(), end=(now + timedelta(days=30)).isoformat(), query=query)
    return rules.say(result, lambda d: _events({**d, "period": f"the next 30 days matching '{query}'"}))


@intent(r"^(?:cancel|delete|remove|drop|clear)\s+(?:my\s+|the\s+)?(?:(?:meeting|event|call|appointment|session)\s+)?"
        r"(?:(?:with|called|titled|about)\s+)?(.+)$", "calendar-cancel")
def _cancel(rules: Rules, m: re.Match, ctx: ToolContext) -> str | None:
    result = rules.call(ctx, "calendar_delete", event=m.group(1).strip())
    if not result.ok and "can't find an event" in (result.error or ""):
        return f"I can't find an event matching \"{m.group(1).strip()}\" in the next few weeks."
    return rules.say(result, lambda d: d["message"])


# =================================================================================
# Email
# =================================================================================

@intent(r"^(?:check|sync|refresh|fetch|get)\s+(?:my\s+)?(?:e-?mails?|mail|inbox)$", "email-sync")
def _email_sync(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    result = rules.call(ctx, "email_sync")
    if not result.ok:
        listing = rules.call(ctx, "email_list", unread_only=True)
        return f"Could not reach the mail server ({result.error}).\n" + rules.say(listing, _emails)
    return f"{result.data['message']}\n" + rules.say(rules.call(ctx, "email_list", unread_only=True), _emails)


@intent(r"^(?:read|open|show)\s+(?:me\s+)?(?:e-?mail|mail)\s*#?(\d+)$|^(?:read|open)\s+#(\d+)$", "email-read")
def _email_read(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    result = rules.call(ctx, "email_read", id=int(m.group(1) or m.group(2)), mark_read=True)

    def fmt(mail: dict[str, Any]) -> str:
        head = [f"From: {mail['from']} <{mail['from_address']}>", f"Subject: {mail['subject']}", f"Date: {mail['date']}"]
        if mail.get("attachments"):
            head.append("Attachments: " + ", ".join(mail["attachments"]))
        return "\n".join(head) + "\n\n" + clip(mail["body"], 3500)
    return rules.say(result, fmt)


@intent(r"^reply(?P<all>\s+all)?\s+(?:to\s+)?(?:e-?mail\s+|mail\s+)?#?(?P<id>\d+)\s*(?:" + _SAY + r"|with)?\s+(?P<body>.+)$",
        "email-reply", verbatim=True)
def _email_reply(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "email_reply", id=int(m.group("id")), body=m.group("body").strip(),
                                reply_all=bool(m.group("all")) or None), lambda d: d["message"])


@intent(r"^(archive|trash|delete)\s+(?:e-?mail|mail)\s+#?(\d+)$", "email-archive")
def _email_archive(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    trash = m.group(1).lower() != "archive"
    return rules.say(rules.call(ctx, "email_archive", id=int(m.group(2)), trash=trash or None), lambda d: d["message"])


@intent(r"^mark\s+(?:e-?mail|mail)\s+#?(\d+)\s+(?:as\s+)?(read|unread|flagged|important|unflagged)$", "email-mark")
def _email_mark(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    word = m.group(2).lower()
    args: dict[str, Any] = {"id": int(m.group(1))}
    if word in ("read", "unread"):
        args["read"] = word == "read"
    else:
        args["flagged"] = word != "unflagged"
    return rules.say(rules.call(ctx, "email_mark", **args), lambda d: d["message"])


@intent(r"^(?:search|find)\s+(?:my\s+|the\s+)?(?:e-?mails?|mail|inbox)\s+(?:for|about)\s+(.+)$", "email-search")
def _email_search(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "email_list", query=m.group(1).strip(), limit=15), _emails)


@intent(r"^(?:send\s+|write\s+|draft\s+)?(?:an?\s+)?(?:e-?mail|mail)\s+(?:to\s+)?(?P<to>.+?)\s*"
        r"(?:\s+(?:with\s+)?subject\s+(?P<subject>.+?))?\s*(?:" + _SAY + r"|with (?:the )?(?:message|body|text))\s*(?P<body>.+)$",
        "email-send", verbatim=True)
def _email_send(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    body = m.group("body").strip()
    first_sentence = re.split(r"(?<=[.!?])\s+|\n", body, maxsplit=1)[0].rstrip(".!?,;: ")
    subject = (m.group("subject") or "").strip() or clip(first_sentence, 60, "")
    recipients = [r.strip() for r in re.split(r"\s*(?:,|\band\b)\s*", m.group("to")) if r.strip()]
    return rules.say(rules.call(ctx, "email_send", to=recipients, subject=subject[:1].upper() + subject[1:], body=body),
                     lambda d: d["message"])


@intent(r"^(?:(?:show|list|read|check|any|do i have(?: any)?|get)\s+)?(?:me\s+)?(?:my\s+|the\s+)?"
        r"(?P<kind>new\s+|unread\s+|latest\s+|recent\s+|important\s+)?(?:e-?mails?|mails?|inbox)"
        r"(?:\s+from\s+(?P<sender>.+?))?(?:\s+(?:about|regarding|on|re)\s+(?P<topic>.+))?$", "email-list")
def _email_list(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    kind = (m.group("kind") or "").strip().lower()
    return rules.say(rules.call(
        ctx, "email_list", unread_only=True if kind in ("new", "unread") else None,
        needs_reply=True if kind == "important" else None, sender=m.group("sender"), query=m.group("topic")), _emails)


# =================================================================================
# WhatsApp and SMS
# =================================================================================

@intent(r"^(?:send\s+)?(?:an?\s+)?(?:sms|text(?: message)?)\s+(?:to\s+)?(?P<to>.+?)\s*" + _SAY_ONE + r"\s*(?P<body>.+)$", "sms-send", verbatim=True)
def _sms_send(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "phone_sms", to=m.group("to").strip(), text=m.group("body").strip()),
                     lambda d: d["message"])


@intent(r"^(?:any\s+|show\s+|check\s+|read\s+)?(?:my\s+)?(?:new\s+|unread\s+)?(?:sms|texts?|text messages)$", "sms-list")
def _sms_list(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "whatsapp_chats", channel="sms"), _chats)


@intent(r"^(?:read|show|open|check)\s+(?:me\s+)?(?:(?:the|my)\s+)?(?:whats\s?app\s+|wa\s+)?"
        r"(?:messages?|chat|conversation|thread)\s+(?:from|with|of)\s+(?P<a>.+)$|"
        r"^what did\s+(?P<b>.+?)\s+(?:say|send|write|want)$|^(?:messages?|chat)\s+(?:from|with)\s+(?P<c>.+)$", "wa-read")
def _wa_read(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    who = (m.group("a") or m.group("b") or m.group("c")).strip()
    result = rules.call(ctx, "whatsapp_read", chat=who, limit=12, mark_read=True)

    def fmt(data: dict[str, Any]) -> str:
        if not data["messages"]:
            return f"No messages in {data['chat']}."
        lines = [f"{data['chat']}:"]
        for message in data["messages"]:
            who_said = "You" if message["from"] == "owner" else message["from"]
            lines.append(f"• {who_said} ({message['when'][11:16]}): {clip(message['text'], 300)}")
        return "\n".join(lines)
    return rules.say(result, fmt)


@intent(r"^(?:(?:show|list|check|read|any|do i have(?: any)?)\s+)?(?:me\s+)?(?:my\s+|the\s+)?(?P<kind>new\s+|unread\s+)?"
        r"(?:whats\s?app|wa)(?:\s+(?:messages?|chats?|inbox))?$|^(?:any\s+|show\s+|check\s+)?(?:my\s+)?(?:new\s+|unread\s+)?messages$|"
        r"^who (?:messaged|texted|pinged) me$", "wa-list")
def _wa_list(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    unread = bool(m.group("kind")) or None
    return rules.say(rules.call(ctx, "whatsapp_chats", unread_only=unread), _chats)


@intent(r"^reply\s+(?:to\s+)?(?P<to>.+?)\s+(?P<wa>on\s+whats\s?app\s+)?(?:saying|with|that|:)\s*(?P<body>.+)$",
        "reply-person", verbatim=True)
def _reply_person(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    """'Reply to Anita saying ...' answers on the channel Anita last wrote on."""
    to, body = m.group("to").strip(), m.group("body").strip()
    if not m.group("wa"):
        mails = rules.app.mail.list(sender=to, limit=1)
        chat = rules.app.whatsapp.find_chat(to)
        last_chat = rules.app.store.scalar(
            "SELECT MAX(ts) FROM messages WHERE chat_id = ? AND direction = 'in'", (chat["id"],), 0) if chat else 0
        if mails and mails[0]["date_ts"] >= (last_chat or 0):
            return rules.say(rules.call(ctx, "email_reply", id=mails[0]["id"], body=body), lambda d: d["message"])
    return rules.say(rules.call(ctx, "whatsapp_send", to=to, text=body), lambda d: d["message"])


@intent(r"^(?:send\s+)?(?:a\s+)?(?:whats\s?app|wa)(?:\s+message)?\s+(?:to\s+)?(?P<to>.+?)\s*" + _SAY_ONE + r"\s*(?P<body>.+)$|"
        r"^(?:message|msg|ping|whatsapp|wa)\s+(?P<to2>.+?)\s*" + _SAY_ONE + r"\s*(?P<body2>.+)$", "wa-send", verbatim=True)
def _wa_send(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    to = (m.group("to") or m.group("to2")).strip()
    body = (m.group("body") or m.group("body2")).strip()
    return rules.say(rules.call(ctx, "whatsapp_send", to=to, text=body), lambda d: d["message"])


@intent(r"^(?:tell|inform|ping|message|whatsapp|wa)\s+(?P<rest>\S.+)$", "wa-tell", verbatim=True)
def _wa_tell(rules: Rules, m: re.Match, ctx: ToolContext) -> str | None:
    to, body = rules.split_name(m.group("rest").strip())
    if not body:
        return None
    return rules.say(rules.call(ctx, "whatsapp_send", to=to, text=body[:1].upper() + body[1:]), lambda d: d["message"])


# =================================================================================
# Phone
# =================================================================================

@intent(r"^(?:find|ring|locate)\s+my\s+(?:phone|mobile)$", "phone-ring", fast=True)
def _phone_ring(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "phone_control", action="ring"), lambda d: "Your phone is ringing loudly for 30 seconds.")


@intent(r"^where(?:'s| is)\s+my\s+(?:phone|mobile)$", "phone-where", fast=True)
def _phone_where(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "phone_control", action="location"), lambda d: d["message"])


@intent(r"^(?:turn\s+|switch\s+)?(?P<a>on|off)\s+(?:the\s+|my\s+)?(?:flashlight|torch|flash)$|"
        r"^(?:turn\s+|switch\s+)?(?:the\s+|my\s+)?(?:flashlight|torch|flash)\s+(?P<b>on|off)$|^(?:flashlight|torch)$",
        "phone-torch", fast=True)
def _torch(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    state = (m.group("a") or m.group("b") or "on").lower()
    return rules.say(rules.call(ctx, "phone_control", action=f"torch_{state}"), lambda d: f"Flashlight {state}.")


@intent(r"^(?:set\s+|create\s+|add\s+)?(?:an?\s+)?alarm\s+(?:for\s+|at\s+)?(.+)$|^wake me(?: up)?\s+(?:at\s+)?(.+)$",
        "phone-alarm", fast=True)
def _alarm(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    text = (m.group(1) or m.group(2)).strip()
    now = rules.app.clock.now()
    waking = bool(m.group(2))
    when = parse_when(text, now, rules.order())
    if when is None or not when.has_time:
        when = parse_when(f"at {text}", now, rules.order())
    if when is None or not when.has_time:
        return "What time should the alarm be? For example: \"set alarm for 6:30am\"."
    explicit = re.search(r"\d\s*(?:a\.?m|p\.?m)\b|\b(?:noon|midnight|morning|evening|night|afternoon)\b|"
                         r"\b(?:1[3-9]|2[0-3]|0\d):\d{2}\b", text, re.I)
    if not explicit:
        # A bare "5" is ambiguous: a wake-up call means the morning, otherwise take the next time the clock shows it.
        hour = when.start.hour % 12
        candidates = [when.start.replace(hour=hour), when.start.replace(hour=hour + 12)]
        if waking:
            when.start = candidates[0]
        else:
            today = [c.replace(year=now.year, month=now.month, day=now.day) for c in candidates]
            upcoming = sorted(c if c > now else c + timedelta(days=1) for c in today)
            when.start = upcoming[0]
    label = when.rest if when.rest and len(when.rest) < 40 else None
    result = rules.call(ctx, "phone_control", action="alarm", hour=when.start.hour, minute=when.start.minute, label=label)
    return rules.say(result, lambda d: f"Alarm set on your phone for {when.start.strftime('%I:%M %p').lstrip('0')}.")


@intent(r"^(?:set\s+|start\s+)?(?:an?\s+)?timer\s+(?:for\s+|of\s+)?(.+)$", "phone-timer", fast=True)
def _timer(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    amount = parse_duration(m.group(1).strip())
    if amount is None or amount.total_seconds() < 1:
        return "How long? For example: \"timer 10 minutes\"."
    return rules.say(rules.call(ctx, "phone_control", action="timer", seconds=int(amount.total_seconds())),
                     lambda d: f"Timer started on your phone: {m.group(1).strip()}.")


@intent(r"^(?:silence|mute)\s+my\s+(?:phone|mobile)$|"
        r"^(?:put\s+|set\s+)?(?:my\s+)?(?:phone|mobile)\s+(?:on\s+|to\s+)?(?P<mode>silent|vibrate|ring|normal|loud)(?:\s+mode)?$",
        "phone-ringer", fast=True)
def _ringer(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    mode = {"ring": "normal", "loud": "normal"}.get((m.group("mode") or "silent").lower(), (m.group("mode") or "silent").lower())
    return rules.say(rules.call(ctx, "phone_control", action="ringer", mode=mode), lambda d: f"Phone set to {mode}.")


@intent(r"^(?:do not disturb|dnd)(?:\s+(?P<a>on|off))?$|^(?:turn\s+)?(?P<b>on|off)\s+(?:do not disturb|dnd)$", "phone-dnd", fast=True)
def _dnd(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    state = (m.group("a") or m.group("b") or "on").lower()
    return rules.say(rules.call(ctx, "phone_control", action=f"dnd_{state}"), lambda d: f"Do not disturb is {state}.")


@intent(r"^(?:set\s+)?(?:phone|mobile)\s+volume\s+(?:to\s+)?(\d{1,3})\s*%?$", "phone-volume", fast=True)
def _phone_volume(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    level = min(100, int(m.group(1)))
    return rules.say(rules.call(ctx, "phone_control", action="volume", level=level), lambda d: f"Phone volume {level}%.")


@intent(r"^(?:phone|mobile)\s+(?:battery|status|charge)$|^(?:how much\s+)?battery\s+(?:on|of|in)\s+my\s+(?:phone|mobile)$|"
        r"^is my (?:phone|mobile) (?:connected|charged|charging|online)$", "phone-status", fast=True)
def _phone_status(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    result = rules.call(ctx, "phone_status")

    def fmt(data: dict[str, Any]) -> str:
        if not data.get("paired"):
            return data["message"]
        parts = [f"{data.get('name', 'Phone')}: {'connected' if data['connected'] else 'not reachable'}"]
        if "battery" in data:
            parts.append(f"battery {data['battery']}%" + (" (charging)" if data.get("charging") else ""))
        if data.get("network"):
            parts.append(f"on {data['network']}")
        if data.get("ringer") and data["ringer"] != "normal":
            parts.append(data["ringer"])
        if data.get("dnd"):
            parts.append("do not disturb on")
        return ", ".join(parts) + "."
    return rules.say(result, fmt)


@intent(r"^open\s+(.+?)\s+on\s+(?:my\s+|the\s+)?(?:phone|mobile)$", "phone-open", fast=True)
def _phone_open(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    name = m.group(1).strip()
    return rules.say(rules.call(ctx, "phone_control", action="open_app", name=name), lambda d: f"Opened {name} on your phone.")


@intent(r"^(?:show\s+|any\s+|check\s+)?(?:my\s+)?(?:phone\s+)?notifications$|^(?:any\s+)?missed calls$|^who called(?: me)?$",
        "phone-notifications")
def _notifications(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    result = rules.call(ctx, "phone_notifications", limit=15)

    def fmt(data: dict[str, Any]) -> str:
        if not data["notifications"]:
            return "No phone notifications in the last 24 hours."
        lines = [f"{plural(len(data['notifications']), 'recent notification')}, {plural(data['missed_calls'], 'missed call')}:"]
        lines += [f"• {n['app']}: {clip(n['title'], 50)}" + (f" - {clip(n['text'], 70)}" if n["text"] else "")
                  for n in data["notifications"]]
        return "\n".join(lines)
    return rules.say(result, fmt)


@intent(r"^(?:call|dial|phone)\s+(?!my\b)(.+)$", "phone-call")
def _call(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "phone_call", to=m.group(1).strip()), lambda d: d["message"])


# =================================================================================
# Laptop
# =================================================================================

@intent(r"^(?:set\s+)?(?:the\s+)?(?:laptop\s+)?volume\s+(?:to\s+|at\s+)?(\d{1,3})\s*%?$", "laptop-volume", fast=True)
def _volume(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "laptop_control", action="volume", level=min(100, int(m.group(1)))), lambda d: d["message"])


@intent(r"^(mute|unmute)(?:\s+(?:the\s+)?(?:laptop|sound|audio|volume|speakers?|computer))?$", "laptop-mute", fast=True)
def _mute(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "laptop_control", action=m.group(1).lower()), lambda d: d["message"])


@intent(r"^lock(?:\s+(?:the\s+|my\s+)?(?:screen|laptop|computer|pc|mac))?$", "laptop-lock", fast=True)
def _lock(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "laptop_control", action="lock"), lambda d: d["message"])


@intent(r"^(?:sleep|suspend|hibernate)(?:\s+(?:the\s+|my\s+)?(?:laptop|computer|pc|mac))?$|"
        r"^put (?:the |my )?(?:laptop|computer|pc|mac) to sleep$", "laptop-sleep", fast=True)
def _sleep(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "laptop_control", action="sleep"), lambda d: d["message"])


@intent(r"^(?:take\s+)?(?:a\s+)?(?:screenshot|screen shot|screen capture)$|^capture (?:the |my )?screen$", "laptop-screenshot", fast=True)
def _screenshot(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "laptop_control", action="screenshot"), lambda d: d["message"])


@intent(r"^(?:what(?:'s| is) (?:on|in) (?:my |the )?clipboard|paste|show (?:the |my )?clipboard)$", "clipboard-get", fast=True)
def _clipboard_get(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "laptop_control", action="clipboard_get"), lambda d: d["text"] or "The clipboard is empty.")


@intent(r"^(?:copy|clipboard)\s*[:\-]?\s+(.+)$", "clipboard-set", fast=True, verbatim=True)
def _clipboard_set(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "laptop_control", action="clipboard_set", text=m.group(1)), lambda d: d["message"])


@intent(r"^(?:find|search for|locate|look for)\s+(?:the\s+|my\s+|a\s+)?(?:files?|documents?|docs?|folders?)\s+"
        r"(?:named\s+|called\s+|matching\s+|about\s+)?(?P<pattern>.+?)(?:\s+in\s+(?P<root>[~/\\.\w].*))?$", "file-find")
def _file_find(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    result = rules.call(ctx, "laptop_files", action="find", pattern=m.group("pattern").strip(), path=m.group("root"))

    def fmt(data: dict[str, Any]) -> str:
        if not data["matches"]:
            return f"No files matching \"{m.group('pattern').strip()}\"."
        lines = [plural(len(data['matches']), "match", "matches") + (" (more exist)" if data.get("truncated") else "") + ":"]
        lines += [f"• {match['path']}" for match in data["matches"][:20]]
        return "\n".join(lines)
    return rules.say(result, fmt)


@intent(r"^(?:list|show)\s+(?:the\s+)?(?:files|folder|directory|contents)(?:\s+(?:in|of|at)\s+(?P<a>.+))?$|^ls(?:\s+(?P<b>.+))?$", "file-list")
def _file_list(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    result = rules.call(ctx, "laptop_files", action="list", path=m.group("a") or m.group("b"))

    def fmt(data: dict[str, Any]) -> str:
        lines = [data["folder"] + ":"] + [f"• {entry['name']}" for entry in data["entries"][:60]]
        if data.get("truncated") or len(data["entries"]) > 60:
            lines.append("...and more.")
        return "\n".join(lines)
    return rules.say(result, fmt)


@intent(r"^(?:read|show|cat|print)\s+(?:the\s+)?file\s+(.+)$", "file-read")
def _file_read(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "laptop_files", action="read", path=m.group(1).strip()),
                     lambda d: clip(d["text"], 4000) + ("\n[cut - the file is longer]" if d["truncated"] else ""))


@intent(r"^(?:run|execute|shell|cmd|terminal)\s*:\s*(.+)$|^(?:run command|execute|shell|cmd)\s+(.+)$|^\$\s*(.+)$", "shell", verbatim=True)
def _shell(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    command = next(g for g in m.groups() if g).strip()

    def fmt(data: dict[str, Any]) -> str:
        text = data["output"].rstrip() or "(no output)"
        if data.get("errors"):
            text += "\n" + data["errors"].rstrip()
        return f"exit {data['exit_code']}\n{text}"
    return rules.say(rules.call(ctx, "laptop_shell", command=command), fmt)


@intent(r"^(?:laptop|computer|system|pc|mac)\s+(?:status|info|battery|health)$|^battery(?:\s+(?:level|status))?$|"
        r"^(?:how much\s+)?(?:disk|storage)(?:\s+space)?(?:\s+(?:free|left))?$", "laptop-status", fast=True)
def _laptop_status(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    result = rules.call(ctx, "laptop_status")

    def fmt(data: dict[str, Any]) -> str:
        parts = [f"Laptop ({data['system']}, {data['host']})"]
        if "battery" in data:
            parts.append(f"battery {data['battery']}%" + (" (charging)" if data.get("charging") else ""))
        if "disk_free_gb" in data:
            parts.append(f"{data['disk_free_gb']} GB free of {data['disk_total_gb']} GB")
        parts.append("online" if data["online"] else "offline")
        text = ", ".join(parts) + "."
        phone = rules.app.phone.status()
        if phone["paired"] and m.group(0).lower().startswith("battery"):
            state = phone["devices"][-1]["state"] or {}
            if "battery" in state:
                text += f" Phone battery {state['battery']}%."
        return text
    return rules.say(result, fmt)


@intent(r"^(?:what(?:'s| is) running|running (?:apps|processes|programs)|processes|top)$", "laptop-processes", fast=True)
def _processes(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "laptop_processes", limit=15), lambda d: "\n".join(d["processes"]))


@intent(r"^(?:open|launch|start)\s+(.+)$|^run\s+(?!command\b)([\w][\w .+-]{0,40})$", "laptop-open", fast=True)
def _open(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    target = (m.group(1) or m.group(2)).strip()
    target = re.sub(r"^(?:the\s+|my\s+)?(?:app\s+|application\s+|website\s+|file\s+|folder\s+)?", "", target, flags=re.I)
    # A link the owner typed out is theirs to open. (A link a model picks waits for approval.)
    if ctx.actor == "owner" and rules.app.laptop.classify_open(target)[0] == "link":
        ctx = ctx.direct()
    return rules.say(rules.call(ctx, "laptop_open", target=target), lambda d: d["message"])


# =================================================================================
# Memory, contacts, search
# =================================================================================

@intent(r"^forget\s+(?:note\s+)?#?(\d+)$", "memory-forget")
def _forget(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "memory_delete", id=int(m.group(1))), lambda d: d["message"])


@intent(r"^(?:my\s+)?(?:notes|memories)$|^what do you remember$", "memory-list")
def _notes(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    notes = rules.app.memory.all(30)
    if not notes:
        return "I have no notes yet. Say \"remember that ...\" to add one."
    return "What I'm keeping in mind:\n" + "\n".join(f"• #{n['id']} {clip(n['text'], 140)}" for n in notes)


@intent(r"^(?:what do you (?:know|remember) about|recall|what did i (?:tell|say to) you about)\s+(.+)$", "memory-recall")
def _recall(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    topic = m.group(1).strip()
    notes = rules.app.memory.recall(topic)
    hits = [h for h in rules.app.store.search(topic, limit=8) if h["kind"] != "note"]
    if not notes and not hits:
        return f"I have nothing on \"{topic}\"."
    lines: list[str] = []
    if notes:
        lines.append("From my notes:")
        lines += [f"• {clip(n['text'], 200)}" for n in notes]
    if hits:
        lines.append("Also mentioned in:")
        lines += [f"• [{h['kind']} #{h['ref']}] {clip(h['title'] or h['excerpt'], 90)}" for h in hits]
    return "\n".join(lines)


@intent(r"^(?:remember|note|keep in mind|memori[sz]e)\s*(?:that\s+|this\s*:?\s*|:\s*)?(.+)$", "memory-save", verbatim=True)
def _remember(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    return rules.say(rules.call(ctx, "memory_save", text=m.group(1).strip()), lambda d: d["message"])


@intent(r"^(?:mark|make|set)\s+(.+?)\s+(?:as\s+)?(?:a\s+)?vip$", "contact-vip")
def _vip(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    matches = rules.app.contacts.resolve(m.group(1).strip())
    if not matches or matches[0].get("id", 0) == 0:
        return f"I don't know a contact called \"{m.group(1).strip()}\"."
    person = matches[0]
    rules.app.store.update("contacts", {"vip": 1}, "id = ?", (person["id"],))
    return f"{person['name']} is now a VIP: their messages rank first."


@intent(r"^(?:save|add|new)\s+contact\s+(.+)$", "contact-save")
def _contact_save(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    rest = m.group(1)
    email = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", rest)
    phone = re.search(r"\+?\d[\d\s()-]{6,}\d", rest)
    name = rest
    for found in (email, phone):
        if found:
            name = name.replace(found.group(0), " ")
    name = re.sub(r"\s+", " ", name).strip(" ,;-")
    if not name:
        return "What is the contact's name?"
    return rules.say(rules.call(ctx, "contact_save", name=name, email=email.group(0) if email else None,
                                phone=phone.group(0) if phone else None), lambda d: d["message"])


@intent(r"^(?:who is|contact(?: details)?(?: for| of)?|(?:phone\s+)?number (?:for|of)|e-?mail(?: address)? (?:for|of))\s+(.+)$", "contact-find")
def _contact(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    result = rules.call(ctx, "contact_find", query=m.group(1).strip())

    def fmt(data: dict[str, Any]) -> str:
        if not data["contacts"]:
            return f"I don't have a contact matching \"{m.group(1).strip()}\"."
        lines = []
        for person in data["contacts"][:5]:
            bits = [person["name"]] + person.get("phones", []) + person.get("emails", [])
            lines.append("• " + " · ".join(bits) + (" [VIP]" if person.get("vip") else ""))
        return "\n".join(lines)
    return rules.say(result, fmt)


@intent(r"^(?:search|find|look up|look for|lookup)\s+(?:for\s+)?(.+)$", "search")
def _search(rules: Rules, m: re.Match, ctx: ToolContext) -> str:
    result = rules.call(ctx, "search_everything", query=m.group(1).strip(), limit=12)

    def fmt(data: dict[str, Any]) -> str:
        if not data["results"]:
            return f"Nothing stored locally mentions \"{m.group(1).strip()}\"."
        lines = [f"{plural(data['count'], 'result')} on this laptop:"]
        for hit in data["results"]:
            who = f" - {hit['who']}" if hit.get("who") else ""
            lines.append(f"• [{hit['kind']} #{hit['id']}] {clip(hit['title'] or hit['excerpt'], 80)}{who}")
        return "\n".join(lines)
    return rules.say(result, fmt)
