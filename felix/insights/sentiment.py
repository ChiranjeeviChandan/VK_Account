"""Offline sentiment, urgency and issue detection for business messages.

A small, transparent lexicon-and-rules analyser. It runs on every email and
chat message with no network, and it reports *why* it flagged something
(``signals``) so the owner can check its reasoning. When Felix is online,
Claude refines the summary; this layer is what keeps "what's broken" working
on a plane.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# Word scores, roughly -3 (very negative) .. +3 (very positive).
LEXICON: dict[str, float] = {
    # negative - feeling
    "angry": -2.6, "furious": -3.0, "outraged": -3.0, "livid": -3.0, "upset": -2.0, "annoyed": -1.8,
    "frustrated": -2.2, "frustrating": -2.2, "disappointed": -2.2, "disappointing": -2.2,
    "unhappy": -2.0, "dissatisfied": -2.2, "concerned": -1.2, "worried": -1.4, "worrying": -1.4,
    "unacceptable": -2.8, "terrible": -2.6, "horrible": -2.6, "awful": -2.6, "worst": -2.8,
    "pathetic": -2.8, "ridiculous": -2.2, "useless": -2.4, "poor": -1.6, "bad": -1.6, "sad": -1.4,
    "disaster": -2.8, "mess": -1.8, "nightmare": -2.6, "shocking": -2.2, "appalling": -2.8,
    "embarrassing": -2.0, "unprofessional": -2.4, "rude": -2.2, "ignored": -2.0, "neglected": -2.0,
    "confused": -1.0, "confusing": -1.2, "stressful": -1.6, "painful": -1.8, "sorry": -0.6,
    "regret": -1.2, "unfortunately": -1.0, "sadly": -1.0, "alarming": -2.0, "fed": -0.4,
    # negative - things going wrong
    "broken": -2.2, "broke": -1.8, "fail": -2.0, "failed": -2.2, "fails": -2.0, "failing": -2.2,
    "failure": -2.4, "error": -1.6, "errors": -1.6, "bug": -1.4, "bugs": -1.4, "crash": -2.2,
    "crashed": -2.4, "crashing": -2.4, "outage": -2.8, "downtime": -2.4, "issue": -1.2, "issues": -1.2,
    "problem": -1.4, "problems": -1.4, "trouble": -1.4, "defect": -1.8, "defective": -2.0,
    "delay": -1.4, "delayed": -1.6, "delays": -1.4, "late": -1.0, "overdue": -1.8, "slipped": -1.6,
    "slipping": -1.6, "stuck": -1.6, "blocked": -1.6, "blocker": -1.8, "missed": -1.6, "missing": -1.2,
    "lost": -1.6, "loss": -1.6, "losses": -1.8, "damage": -1.8, "damaged": -2.0, "wrong": -1.4,
    "incorrect": -1.4, "mistake": -1.4, "risk": -1.0, "risky": -1.2, "threat": -1.8, "breach": -2.4,
    "hacked": -2.8, "fraud": -2.8, "scam": -2.6, "leak": -2.0, "leaked": -2.2, "compromised": -2.6,
    "complaint": -2.0, "complaints": -2.0, "complain": -1.8, "complaining": -1.8, "escalate": -1.8,
    "escalated": -2.0, "escalating": -2.0, "escalation": -2.0, "dispute": -1.8, "penalty": -1.8,
    "lawsuit": -2.8, "sue": -2.6, "terminate": -2.2, "termination": -2.2, "cancel": -1.4,
    "cancelled": -1.4, "canceled": -1.4, "cancellation": -1.6, "refund": -1.6, "chargeback": -2.2,
    "reject": -1.6, "rejected": -1.8, "declined": -1.4, "denied": -1.4, "unpaid": -1.8,
    "resign": -1.8, "resigned": -2.0, "resignation": -2.0, "quit": -1.8, "layoff": -2.2,
    "unable": -1.2, "cannot": -0.8, "slow": -1.2, "unstable": -1.8, "unreliable": -2.0,
    "critical": -1.2, "severe": -1.8, "serious": -1.2, "emergency": -1.8, "crisis": -2.6,
    "shortfall": -1.8, "shortage": -1.6, "decline": -1.2, "dropped": -1.0, "churn": -1.8,
    "harassment": -2.8, "grievance": -2.0, "violation": -2.2, "warning": -1.2, "difficult": -1.0,
    # positive
    "great": 2.2, "excellent": 2.8, "amazing": 2.8, "awesome": 2.6, "fantastic": 2.8, "wonderful": 2.6,
    "good": 1.6, "nice": 1.4, "happy": 2.0, "glad": 1.8, "pleased": 2.0, "delighted": 2.6,
    "thrilled": 2.8, "love": 2.4, "loved": 2.4, "perfect": 2.6, "brilliant": 2.6, "superb": 2.6,
    "thanks": 1.4, "thank": 1.4, "thankful": 1.8, "grateful": 2.0, "appreciate": 1.8,
    "appreciated": 1.8, "congrats": 2.4, "congratulations": 2.4, "kudos": 2.2, "impressed": 2.2,
    "impressive": 2.2, "success": 2.0, "successful": 2.0, "successfully": 1.8, "win": 2.0, "won": 2.2,
    "achieved": 1.8, "milestone": 1.6, "record": 1.2, "growth": 1.4, "improved": 1.6,
    "improvement": 1.4, "resolved": 1.8, "fixed": 1.6, "solved": 1.8, "sorted": 1.4, "approved": 1.6,
    "signed": 1.6, "closed": 0.6, "launched": 1.6, "live": 0.8, "smooth": 1.4, "smoothly": 1.4,
    "ahead": 0.8, "exceeded": 2.0, "renewed": 1.8, "renewal": 0.8, "welcome": 1.0, "excited": 2.0,
    "exciting": 2.0, "positive": 1.4, "helpful": 1.6, "works": 0.8, "working": 0.6, "recovered": 1.4,
    "restored": 1.6, "stable": 1.0, "confirmed": 0.8, "best": 1.6, "strong": 1.2, "profit": 1.4,
    # Hinglish / romanised Hindi seen in everyday business chat
    "kharab": -1.8, "bekar": -2.0, "bakwas": -2.2, "ganda": -1.6, "dikkat": -1.4, "pareshan": -1.8,
    "pareshani": -1.8, "galat": -1.4, "nuksan": -1.8, "gussa": -2.0, "ghatiya": -2.4, "mushkil": -1.2,
    "badhiya": 2.0, "shukriya": 1.6, "dhanyavaad": 1.6, "dhanyawad": 1.6, "mast": 1.8, "accha": 1.2,
    "acha": 1.2, "sahi": 1.0, "shandar": 2.2, "zabardast": 2.4, "khush": 1.8,
}

NEGATORS = {
    "not", "no", "never", "none", "nobody", "nothing", "neither", "nor", "cannot", "cant", "wont",
    "dont", "doesnt", "didnt", "isnt", "arent", "wasnt", "werent", "hasnt", "havent", "hadnt",
    "shouldnt", "wouldnt", "couldnt", "without", "hardly", "barely", "nahi", "nahin", "nai", "mat", "na",
}
INTENSIFIERS = {
    "very": 1.4, "extremely": 1.7, "really": 1.3, "so": 1.25, "totally": 1.4, "absolutely": 1.5,
    "completely": 1.5, "highly": 1.4, "deeply": 1.4, "incredibly": 1.6, "utterly": 1.6, "too": 1.2,
    "quite": 1.15, "seriously": 1.4, "bahut": 1.4, "bohot": 1.4, "ekdum": 1.5, "kaafi": 1.2,
}
DIMINISHERS = {"slightly": 0.6, "somewhat": 0.7, "bit": 0.7, "little": 0.7, "minor": 0.6, "small": 0.7, "thoda": 0.7}

EMOJI = {
    "😡": -2.8, "🤬": -3.0, "😠": -2.4, "😤": -2.0, "😞": -1.6, "😔": -1.4, "😢": -1.6, "😭": -1.8,
    "👎": -2.0, "❌": -1.4, "🚨": -1.8, "⚠️": -1.4, "⚠": -1.4, "😱": -1.8, "💔": -1.8, "🤦": -1.4,
    "🙄": -1.4, "😒": -1.4, "👍": 1.6, "🎉": 2.4, "✅": 1.2, "😊": 1.8, "😀": 1.6, "😃": 1.6,
    "❤️": 2.2, "❤": 2.2, "🙌": 2.0, "👏": 2.0, "🥳": 2.4, "💯": 1.8, "🚀": 1.6, "😍": 2.2, "🙂": 1.0,
}

# (category, weight toward "this is broken", pattern)
CATEGORY_PATTERNS: list[tuple[str, float, re.Pattern[str]]] = [
    ("incident", 0.40, re.compile(
        r"\b(?:outage|down\s?time|(?:is|are|went|goes|gone|still|been|keeps?)\s+down|not\s+working|isn'?t\s+working|"
        r"stopped\s+working|doesn'?t\s+work|not\s+loading|not\s+responding|unresponsive|crash(?:ed|es|ing)?|"
        r"production\s+(?:issue|incident|bug|is\s+down)|sev(?:erity)?[\s-]?[012]\b|\bp[01]\b|incident|"
        r"(?:server|site|website|app|api|portal|system|service|gateway|network|database|checkout|payments?)\s+"
        r"(?:is|are|was|went|has\s+been|keeps?)\s+(?:down|failing|broken|offline|unavailable|slow|crashing)|"
        r"unable\s+to\s+(?:log\s?in|access|pay|check\s?out|connect|place)|can(?:'?t|not)\s+(?:log\s?in|access|pay|connect)|"
        r"error\s+(?:code\s+)?\d{3}|50[0234]\s+error|time(?:d|s)?[\s-]?out|data\s+loss|rollback|rolled\s+back|"
        r"nahi\s+chal\s+rah[ai]|kaam\s+nahi\s+kar\s+rah[ai]|band\s+(?:ho\s+gay[ai]|hai|pad[ai])|chal\s+nahi\s+rah[ai])\b", re.I)),
    ("security", 0.48, re.compile(
        r"\b(?:security\s+(?:breach|incident|issue)|data\s+breach|hack(?:ed|ing)?|phishing|ransomware|malware|"
        r"compromised|unauthori[sz]ed\s+(?:access|login|transaction)|suspicious\s+(?:login|activity|transaction)|"
        r"credentials?\s+(?:leak|leaked|exposed|stolen)|data\s+leak|fraud(?:ulent)?)\b", re.I)),
    ("legal", 0.42, re.compile(
        r"\b(?:legal\s+(?:notice|action|proceedings?)|law\s?suit|litigation|court|subpoena|summons|show[\s-]cause|"
        r"cease\s+and\s+desist|breach\s+of\s+contract|non[\s-]?complian(?:t|ce)|regulator(?:y)?\s+(?:notice|action|inquiry)|"
        r"tax\s+notice|audit\s+(?:finding|observation|qualification)|penalt(?:y|ies)\s+(?:notice|imposed)|our\s+lawyers?)\b", re.I)),
    ("churn", 0.36, re.compile(
        r"\b(?:cancel(?:ling|ing|lation)?\s+(?:our|the|my|this)?\s*(?:contract|subscription|order|account|plan|agreement|deal)|"
        r"terminat(?:e|ing|ion)\s+(?:our|the|this)?\s*(?:contract|agreement|engagement|services?)|not\s+(?:going\s+to\s+)?renew(?:ing)?|"
        r"won'?t\s+(?:be\s+)?renew(?:ing)?|switch(?:ing)?\s+to\s+(?:a\s+|another\s+)?(?:competitor|vendor|provider|supplier)|"
        r"mov(?:e|ing)\s+to\s+(?:a\s+|another\s+)?(?:competitor|vendor|provider)|reconsider(?:ing)?\s+(?:our|the|this)\s+"
        r"(?:contract|partnership|relationship|engagement)|(?:want|need|demand|request(?:ing)?)\s+(?:a\s+)?(?:full\s+)?refund|"
        r"refund\s+(?:my|our|the)|charge\s?back|take\s+(?:our|my)\s+business\s+elsewhere|last\s+chance|walk\s+away)\b", re.I)),
    ("complaint", 0.28, re.compile(
        r"\b(?:complain(?:t|ts|ing)?|unacceptable|not\s+(?:at\s+all\s+)?(?:happy|satisfied|acceptable|okay|ok)|"
        r"(?:very|extremely|deeply|really|quite)\s+(?:disappointed|unhappy|upset|frustrated|concerned)|fed\s+up|"
        r"poor\s+(?:service|quality|support|response|experience)|no\s+(?:response|reply|update|one\s+(?:has\s+)?(?:replied|responded|called))|"
        r"still\s+(?:waiting|no\s+(?:update|response|reply))|(?:second|third|fourth|2nd|3rd|4th|nth)\s+(?:time|reminder|follow[\s-]?up)|"
        r"again\s+and\s+again|how\s+many\s+times|(?:multiple|several|repeated)\s+(?:times|reminders|follow[\s-]?ups|emails|calls)|"
        r"being\s+ignored|this\s+is\s+(?:ridiculous|unacceptable|not\s+done)|worst\s+(?:experience|service)|"
        r"bahut\s+(?:kharab|bekar|ganda)|koi\s+(?:jawab|reply|response)\s+nahi)\b", re.I)),
    ("blocker", 0.22, re.compile(
        r"\b(?:block(?:ed|er|ing)|stuck|bottleneck|on\s+hold|at\s+risk|behind\s+schedule|slipp(?:ed|ing)|"
        r"miss(?:ed|ing)?\s+(?:the\s+|our\s+)?(?:deadline|milestone|target|sla|delivery)|won'?t\s+(?:make|meet)\s+(?:it|the)|"
        r"delay(?:ed|s)?|can(?:'?t|not)\s+proceed|waiting\s+(?:on|for)\s+(?:your|you|approval|sign[\s-]?off|the\s+go[\s-]ahead)|"
        r"pending\s+(?:your|from\s+you|approval|sign[\s-]?off|decision)|need\s+(?:a\s+)?decision|atk[ae]\s+hu[ae]|ruk[ae]\s+hu[ae])\b", re.I)),
    ("payment", 0.22, re.compile(
        r"\b(?:overdue|past\s+due|unpaid|outstanding\s+(?:amount|invoice|payment|dues?|balance)|payment\s+(?:failed|pending|due|"
        r"not\s+received|declined|bounced|reminder|overdue)|invoice\s+(?:is\s+)?(?:overdue|pending|unpaid|due)|cheque\s+bounced|"
        r"insufficient\s+funds|late\s+fee|cash\s?flow\s+(?:issue|problem|crunch)|dues?\s+(?:pending|not\s+cleared)|"
        r"paisa\s+nahi\s+aay[ai]|payment\s+nahi\s+(?:aay[ai]|hu[ai]))\b", re.I)),
    ("people", 0.26, re.compile(
        r"\b(?:resign(?:ed|ing|ation)?|put\s+in\s+(?:my|his|her|their)\s+papers|quit(?:ting)?|notice\s+period|"
        r"harass(?:ed|ment)|grievance|misconduct|burn(?:ed|t)?\s?out|attrition|lay[\s-]?offs?|let\s+go|"
        r"toxic|morale\s+is\s+(?:low|down)|team\s+is\s+(?:unhappy|demotivated|frustrated)|counter[\s-]?offer)\b", re.I)),
    ("win", -0.30, re.compile(
        r"\b(?:great\s+(?:job|work|news|call|meeting|session)|well\s+done|congrat(?:s|ulations)|kudos|"
        r"thank(?:s|\s+you)\s+(?:so\s+much|a\s+lot|for\s+(?:the|your))|really\s+appreciate|"
        r"(?:deal|contract|order|po)\s+(?:is\s+)?(?:signed|closed|won|confirmed|approved)|(?:signed|closed|won)\s+the\s+(?:deal|contract)|"
        r"went\s+live|go[\s-]?live\s+(?:was\s+)?success|launched\s+successfully|record\s+(?:sales|revenue|month|quarter)|"
        r"ahead\s+of\s+(?:schedule|plan|target)|exceeded\s+(?:the\s+)?(?:target|expectations?)|renewed|funding\s+(?:approved|closed)|"
        r"payment\s+received|bahut\s+badhiya|shandar)\b", re.I)),
    ("resolved", -0.35, re.compile(
        r"\b(?:(?:issue|problem|bug|incident|it|this)\s+(?:is|has\s+been|was|got)\s+(?:now\s+)?(?:resolved|fixed|sorted|closed)|"
        r"resolved\s+now|fixed\s+now|all\s+good\s+now|working\s+(?:fine\s+)?(?:now|again)|back\s+(?:up|online|to\s+normal)|"
        r"(?:is|are)\s+(?:up|live)\s+again|restored|no\s+(?:longer|more)\s+(?:an\s+)?(?:issue|problem)|"
        r"thanks?\s+for\s+(?:fixing|resolving|sorting)|theek\s+ho\s+gay[ai]|chal\s+rah[ai]\s+hai\s+ab)\b", re.I)),
    ("ask", 0.0, re.compile(
        r"(?:\b(?:can|could|would|will)\s+you\b|\bplease\s+(?:send|share|confirm|review|approve|advise|check|call|reply|respond|"
        r"let|provide|arrange|sign|look|update|forward|revert|do)\b|\bkindly\b|\bneed\s+(?:you|your)\b|\blet\s+me\s+know\b|"
        r"\byour\s+(?:thoughts|approval|input|decision|sign[\s-]?off|feedback|confirmation|views?|go[\s-]ahead)\b|"
        r"\bwaiting\s+(?:on|for)\s+you(?:r)?\b|\bget\s+back\s+to\s+(?:me|us)\b|\brequest(?:ing)?\s+you\b|\bare\s+you\s+(?:free|available)\b|"
        r"\bwhat\s+do\s+you\s+think\b|\bplease\s+revert\b|\bneed\s+(?:the|this|that|it|these|those|an?|some)\b|"
        r"\bplease[\s.!]*$|\bbata(?:o|iye|na)\b|\bkar\s+d(?:o|ijiye|ena)\b|\bbhej\s+d(?:o|ijiye|ena)\b)", re.I)),
]

URGENCY_PATTERNS: list[tuple[float, re.Pattern[str]]] = [
    (0.55, re.compile(r"\b(?:urgent(?:ly)?|asap|a\.s\.a\.p|immediately|right\s+(?:away|now)|emergency|critical|top\s+priority|"
                      r"highest\s+priority|time[\s-]sensitive|escalat(?:e|ed|ing|ion)|final\s+(?:notice|reminder|warning)|"
                      r"last\s+(?:reminder|warning|chance)|sev(?:erity)?[\s-]?[01]\b|\bp0\b|jaldi|turant|abhi\s+(?:chahiye|karo|bhejo)|fauran)\b", re.I)),
    (0.35, re.compile(r"\b(?:today|tonight|by\s+(?:eod|cob|end\s+of\s+(?:the\s+)?day|tomorrow|noon|morning|evening|\d{1,2}\s*(?:am|pm))|"
                      r"within\s+(?:the\s+)?(?:next\s+)?(?:\d+|an|one|two|a\s+few)\s+(?:hours?|minutes?|mins?)|before\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|"
                      r"deadline|due\s+(?:today|tomorrow|now)|as\s+soon\s+as\s+possible|at\s+the\s+earliest|priority|important|aaj\s+hi)\b", re.I)),
    (0.18, re.compile(r"\b(?:soon|quick(?:ly)?|this\s+week|by\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
                      r"reminder|follow(?:ing)?[\s-]?up|waiting|pending|still)\b", re.I)),
]

BULK_SENDER = re.compile(
    r"(?:^|[._+-])(?:no[\s._-]?reply|do[\s._-]?not[\s._-]?reply|newsletter|news|marketing|promo(?:tions)?|offers?|deals?|"
    r"notifications?|mailer|digest|updates?|info|hello|team|support-noreply|billing-noreply)(?:[._+-]|@)", re.I)
BULK_BODY = re.compile(
    r"\b(?:unsubscribe|view\s+(?:this\s+email\s+)?in\s+(?:your\s+)?browser|manage\s+(?:your\s+)?(?:email\s+)?preferences|"
    r"you(?:'re|\s+are)\s+receiving\s+this|opt[\s-]?out|%\s+off|limited\s+time\s+offer|shop\s+now|buy\s+now)\b", re.I)
ALERT_SENDER = re.compile(
    r"(?:alert|alerts|monitor|monitoring|pagerduty|opsgenie|statuspage|sentry|datadog|grafana|nagios|uptime|pingdom|"
    r"newrelic|cloudwatch|incident|noc|soc)", re.I)

_QUOTE_START = re.compile(
    r"^(?:on\s.+\bwrote:|-{2,}\s*original\s+message\s*-{2,}|-{2,}\s*forwarded\s+message\s*-{2,}|from:\s.+|"
    r"sent\s+from\s+my\s+\w+|get\s+outlook\s+for\s+\w+|_{5,}|\*{5,})\s*$", re.I)
_TOKEN = re.compile(r"[a-zA-Z][a-zA-Z']*|[!?]+", re.UNICODE)


@dataclass
class Analysis:
    sentiment: float = 0.0          # -1 .. 1
    urgency: float = 0.0            # 0 .. 1
    severity: float = 0.0           # 0 .. 1, how strongly this says "something is broken"
    categories: list[str] = field(default_factory=list)
    is_question: bool = False
    asks: bool = False
    bulk: bool = False
    signals: list[str] = field(default_factory=list)   # the phrases that drove the result

    @property
    def label(self) -> str:
        if self.sentiment <= -0.35:
            return "negative"
        if self.sentiment >= 0.35:
            return "positive"
        return "neutral"


def strip_quoted(text: str) -> str:
    """Keep only what the sender wrote this time: drop quoted history,
    forwarded headers and signatures so old anger is not counted again."""
    lines: list[str] = []
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if _QUOTE_START.match(stripped):
            break
        if stripped in ("--", "-- ", "—"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def analyze(text: str, title: str = "", sender: str = "", automated: bool = False) -> Analysis:
    """Score one message together with its subject line, if it has one."""
    body = strip_quoted(text or "")[:6000]
    full = f"{title}. {body}" if title else body
    result = Analysis()
    if not full.strip():
        return result

    result.bulk = bool(automated or BULK_SENDER.search(sender or "") or BULK_BODY.search(body))
    is_alert = bool(ALERT_SENDER.search(sender or ""))

    # --- word-level sentiment with negation and intensity -------------------
    tokens = [t.lower().replace("'", "") for t in _TOKEN.findall(full)]
    total = 0.0
    hits = 0
    for index, token in enumerate(tokens):
        score = LEXICON.get(token)
        if score is None:
            continue
        window = tokens[max(0, index - 3):index]
        multiplier = 1.0
        negated = False
        for prev in window:
            if prev in NEGATORS:
                negated = not negated
            elif prev in INTENSIFIERS:
                multiplier *= INTENSIFIERS[prev]
            elif prev in DIMINISHERS:
                multiplier *= DIMINISHERS[prev]
        if negated:
            # "not working" is bad news; "no problem" is mild good news
            score = -0.75 * score if score < 0 else -0.9 * score - 0.6
        total += score * multiplier
        hits += 1
        if abs(score) >= 1.8:
            result.signals.append(("not " if negated else "") + token)

    for symbol, score in EMOJI.items():
        count = full.count(symbol)
        if count:
            total += score * min(count, 3)
            hits += 1

    exclaims = full.count("!")
    words = [w for w in re.findall(r"[A-Za-z]{3,}", body)]
    shouted = sum(1 for w in words if w.isupper())
    emphasis = 1.0 + min(exclaims, 4) * 0.06 + (0.15 if words and shouted / len(words) > 0.3 else 0.0)

    # --- categories ------------------------------------------------------------
    category_weight = 0.0
    low = full.lower()
    for name, weight, pattern in CATEGORY_PATTERNS:
        match = pattern.search(low)
        if not match:
            continue
        result.categories.append(name)
        phrase = re.sub(r"\s+", " ", match.group(0)).strip()
        if name not in ("ask",) and phrase not in result.signals:
            result.signals.append(phrase)
        category_weight += weight
        if name in ("incident", "security", "legal", "churn", "complaint"):
            total -= 1.6       # these phrases are negative even with no "feeling" words
            hits += 1
        elif name in ("blocker", "payment", "people"):
            total -= 0.9
            hits += 1
        elif name in ("win", "resolved"):
            total += 1.6
            hits += 1

    result.is_question = "?" in body or bool(re.search(r"^\s*(?:who|what|when|where|why|how|can|could|would|will|is|are|do|does|did|should|kya|kab|kaise|kyun)\b", body.strip(), re.I))
    result.asks = "ask" in result.categories or result.is_question

    # --- urgency -----------------------------------------------------------------
    urgency = 0.0
    for weight, pattern in URGENCY_PATTERNS:
        match = pattern.search(low)
        if match:
            urgency += weight
            if weight >= 0.35:
                phrase = re.sub(r"\s+", " ", match.group(0)).strip()
                if phrase not in result.signals:
                    result.signals.append(phrase)
    if total < 0:
        urgency += min(exclaims, 3) * 0.05   # "!!!" only adds pressure to bad news
    result.urgency = round(min(1.0, urgency), 3)

    # --- combine -------------------------------------------------------------------
    if hits:
        # Normalising by sqrt keeps one strong phrase from being drowned in a long email.
        result.sentiment = round(math.tanh((total * emphasis) / (2.2 + math.sqrt(hits))), 3)
    negativity = max(0.0, -result.sentiment)
    broken_weight = max(0.0, category_weight)
    severity = 0.35 * negativity + 0.2 * result.urgency + min(0.6, broken_weight)
    if "resolved" in result.categories and result.sentiment >= 0:
        severity *= 0.25
    if result.bulk and not is_alert:
        severity *= 0.15      # marketing mail is never "broken", whatever its wording
        result.asks = False
    if is_alert and "incident" in result.categories:
        severity = max(severity, 0.6)
        if "alert" not in result.categories:
            result.categories.append("alert")
    result.severity = round(max(0.0, min(1.0, severity)), 3)
    result.signals = result.signals[:8]
    return result


_STOP = set("""
a an the and or but if then else for to of in on at by with from as is are was were be been being it its this that
these those i you he she we they me my your our their his her them us not no yes do does did have has had will would
can could should shall may might must just very so too also about into over under after before more most some any
all each per via re fw fwd hi hello dear regards thanks thank please kindly best team sir madam mr mrs ms dr
get got one two new now today tomorrow yesterday still again here there what when where which who whom why how
am pm ok okay hai ka ki ke ko se me mein par aur ya nahi kya ho raha rahi kar karo
""".split())


def keywords(text: str, limit: int = 6) -> list[str]:
    """The few words that say what a message is about (for grouping)."""
    counts: dict[str, int] = {}
    for word in re.findall(r"[A-Za-z][A-Za-z0-9&-]{2,}", (text or "").lower()):
        if word in _STOP or word in LEXICON:
            continue
        stem = word[:-1] if word.endswith("s") and len(word) > 4 and not word.endswith("ss") else word
        counts[stem] = counts.get(stem, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    return [word for word, _ in ranked[:limit]]


def clean_subject(subject: str) -> str:
    """'RE: Fwd: [EXT] Invoice 42' -> 'Invoice 42' (reply prefixes removed, case kept)."""
    text = subject or ""
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"^\s*(?:re|fw|fwd|aw|sv|tr|答复|回复)\s*(?:\[\d+\])?\s*[:：]\s*", "", text, flags=re.I)
        text = re.sub(r"^\s*\[[^\]]{1,20}\]\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_subject(subject: str) -> str:
    """Thread grouping key: the cleaned subject, lower-cased."""
    return clean_subject(subject).lower()
