"""The offline analyser on realistic business messages.

Each case: (title, text, sender, expected label, categories that must be
found, severity band). Bands are deliberately wide - the point is that
outages rank above grumbles, which rank above routine mail.
"""

import pytest

from felix.insights.sentiment import analyze, keywords, normalize_subject, strip_quoted

HIGH, MID, LOW = (0.6, 1.0), (0.3, 0.8), (0.0, 0.25)

CASES = [
    # --- broken: incidents ---
    ("Payment gateway down", "Checkout is failing for all customers since 9am. We are losing orders every minute. Need a fix urgently!",
     "ops@corp.com", "negative", {"incident"}, HIGH),
    ("", "Sir the billing server is down again, nobody can raise invoices. Third time this week.",
     "", "negative", {"incident", "complaint"}, HIGH),
    ("[ALERT] API latency critical", "api-gateway is unavailable in ap-south-1. Error 503 for 12% of requests.",
     "alerts@pagerduty.com", "negative", {"incident", "alert"}, HIGH),
    ("", "App not working since morning, customers are calling non stop", "", "negative", {"incident"}, MID),
    ("", "website nahi chal raha hai, jaldi dekho", "", "negative", {"incident"}, MID),
    # --- broken: customers ---
    ("Extremely disappointed with the service", "This is unacceptable. I have followed up three times and still no response. "
     "If this is not fixed by tomorrow we will cancel our contract and move to another vendor.",
     "cto@bigcustomer.com", "negative", {"complaint", "churn"}, HIGH),
    ("Refund request", "I want a full refund. The product stopped working after two days and your support has been useless.",
     "angry@customer.com", "negative", {"churn", "incident"}, HIGH),
    ("", "Very poor service 😡 worst experience ever", "", "negative", {"complaint"}, MID),
    ("", "bahut kharab service hai, koi reply nahi karta", "", "negative", {"complaint"}, MID),
    # --- broken: legal / security / money / people ---
    ("Legal notice", "Please treat this as a formal legal notice for breach of contract. Our lawyers will initiate proceedings within 7 days.",
     "legal@firm.com", "negative", {"legal"}, HIGH),
    ("Suspicious login detected", "We noticed unauthorized access to the admin console from an unknown IP. Credentials may be compromised.",
     "security@corp.com", "negative", {"security"}, HIGH),
    ("Invoice 4471 overdue", "Your invoice is 45 days overdue. This is the final reminder before we suspend services.",
     "accounts@vendor.com", "negative", {"payment"}, MID),
    ("Resignation", "I have decided to resign from my position. My last working day will be 30th November.",
     "priya@corp.com", "negative", {"people"}, MID),
    ("Launch slipping", "We are blocked on the vendor API and will miss the deadline. Launch is at risk unless we get a decision today.",
     "pm@corp.com", "negative", {"blocker"}, MID),
    # --- good news ---
    ("We won the Acme deal!", "Great news - the contract is signed. Congratulations to the whole team, fantastic work!",
     "sales@corp.com", "positive", {"win"}, LOW),
    ("", "Thank you so much for the quick turnaround, really appreciate it 🙏", "", "positive", {"win"}, LOW),
    ("Re: Payment gateway down", "The issue has been resolved and checkout is working fine now. Thanks for the patience.",
     "ops@corp.com", "positive", {"resolved"}, LOW),
    ("", "badhiya kaam kiya team, shandar!", "", "positive", set(), LOW),
    # --- routine ---
    ("Meeting notes", "Attaching the notes from today's review. Next sync is on Thursday.", "ea@corp.com", "neutral", set(), LOW),
    ("", "Can you share the deck before the call?", "", "neutral", {"ask"}, LOW),
    ("Lunch tomorrow?", "Are you free for lunch tomorrow around 1?", "friend@gmail.com", "neutral", {"ask"}, LOW),
    ("", "ok", "", "neutral", set(), LOW),
    ("", "No problem at all, happy to help", "", "positive", set(), LOW),
    ("", "No issues from our side, all good", "", "positive", set(), LOW),
    # --- marketing must never look broken ---
    ("LAST CHANCE!!! 70% off ends tonight", "Don't miss out! Shop now. Unsubscribe here.",
     "offers@shop.com", "neutral", set(), LOW),
    ("Your weekly digest", "Top stories: crisis in markets, outage at a cloud provider. Manage your preferences or unsubscribe.",
     "newsletter@news.com", "negative", set(), LOW),
]


@pytest.mark.parametrize("title,text,sender,label,categories,band", CASES)
def test_cases(title, text, sender, label, categories, band):
    result = analyze(text, title=title, sender=sender)
    shown = f"{title!r} / {text[:50]!r}: sentiment={result.sentiment} severity={result.severity} cats={result.categories} signals={result.signals}"
    if label != "neutral" or not result.bulk:
        if label == "neutral":
            assert -0.5 < result.sentiment < 0.5, shown
        else:
            assert result.label == label, shown
    assert categories <= set(result.categories), shown
    assert band[0] <= result.severity <= band[1], shown


def test_ranking_matches_intuition():
    outage = analyze("Production is down, customers cannot log in. URGENT.", title="P0 outage")
    grumble = analyze("The report was a bit late again, slightly annoying.")
    routine = analyze("Sharing the agenda for Monday.")
    praise = analyze("Fantastic quarter, well done everyone!")
    assert outage.severity > grumble.severity > routine.severity >= praise.severity
    assert outage.urgency > 0.5 and routine.urgency == 0


def test_negation():
    assert analyze("the fix is not working").sentiment < 0
    assert analyze("this is not good").sentiment < 0
    assert analyze("no complaints, not a problem").sentiment > 0
    assert analyze("I am not happy with this").sentiment < 0


def test_quoted_history_is_not_counted_twice():
    reply = (
        "All sorted now, thanks!\n\n"
        "On Tue, 10 Nov 2026 at 09:12, Ravi <ravi@corp.com> wrote:\n"
        "> This is unacceptable, the server is down and nobody is responding!!!\n"
    )
    assert strip_quoted(reply) == "All sorted now, thanks!"
    assert analyze(reply).sentiment > 0
    signature = "Please review.\n-- \nRavi Kumar\nThis email may contain a complaint about failure"
    assert strip_quoted(signature) == "Please review."


def test_signals_explain_the_score():
    result = analyze("This is unacceptable, we will cancel our contract.")
    joined = " | ".join(result.signals)
    assert "unacceptable" in joined and "cancel our contract" in joined


def test_question_and_ask_detection():
    assert analyze("Could you approve the budget by Friday?").asks
    assert analyze("What time is the board call").is_question
    assert not analyze("FYI the board call moved to 4pm.").asks


def test_keywords_and_subject_normalisation():
    assert normalize_subject("RE: Fwd: [EXT] Payment Gateway Down") == "payment gateway down"
    assert normalize_subject("Re[2]: AW: hello") == "hello"
    words = keywords("Payment gateway down - payments failing at checkout for gateway users")
    assert words[:2] == ["gateway", "payment"]
