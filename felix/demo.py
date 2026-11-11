"""Sample data for trying Felix before connecting real accounts.

`felix demo` fills a separate, throw-away data folder with a believable
executive's day: mail (including an outage and an unhappy customer), WhatsApp
chats, meetings, to-dos and a draft waiting for approval. Nothing here touches
real accounts, and nothing is ever sent.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import TYPE_CHECKING

from .connectors.mail import Account
from .tools import ToolContext

if TYPE_CHECKING:  # pragma: no cover
    from .app import Felix

DEMO_ACCOUNT = {"id": "demo", "email": "you@northwind.example", "imap_host": "demo.invalid", "smtp_host": "demo.invalid"}

EMAILS = [
    # (hours ago, from, subject, body, extra headers, read)
    (5.0, "Ops Desk <ops@northwind.example>", "Payment gateway down",
     "Checkout has been failing for all customers since 9:10. We are losing orders every minute and the "
     "provider has not responded. Need a decision on switching to the backup gateway urgently!", {}, False),
    (3.2, "Anita Rao <anita.rao@bigretail.example>", "Extremely disappointed with the rollout",
     "This is unacceptable. This is my third follow-up and I still have no response on the missed go-live. "
     "If we do not have a firm date by tomorrow we will cancel our contract and move to another vendor.", {}, False),
    (6.5, "Vendor Accounts <ar@cloudhost.example>", "Invoice 4471 overdue",
     "Your invoice 4471 is 45 days overdue. This is the final reminder before services are suspended.", {}, True),
    (2.1, "Karan Shah <karan@northwind.example>", "We won the Acme deal!",
     "Great news - Acme signed this morning. Three-year contract, the largest in our history. "
     "Congratulations to the whole team, fantastic work!", {}, False),
    (1.4, "Meera Iyer <meera@northwind.example>", "Q4 budget",
     "Hi,\n\nCan you approve the Q4 budget by Friday? Finance needs it to release hiring.\n\nThanks,\nMeera", {}, False),
    (0.6, "Priya Nair <priya@northwind.example>", "Board deck - draft 3",
     "Draft 3 of the board deck is attached. Please review the revenue slide before Thursday's call.", {}, False),
    (7.5, "Daniel Park <daniel@lumen-vc.example>", "Catching up next week?",
     "Would love to hear how the quarter closed. Are you free for coffee on Tuesday or Wednesday?", {}, True),
    (9.0, "Travel Desk <travel@northwind.example>", "Itinerary: Mumbai 18-19 Nov",
     "Your flights and hotel for Mumbai are confirmed. Itinerary attached.", {}, True),
    (4.0, "Weekly Digest <newsletter@trade-wire.example>", "This week in retail tech",
     "Top stories this week. Manage your preferences or unsubscribe.",
     {"List-Unsubscribe": "<mailto:unsub@trade-wire.example>"}, False),
    (0.3, "Ops Desk <ops@northwind.example>", "Re: Payment gateway down",
     "Update: provider confirms a regional outage on their side. ETA unknown. Backup gateway is tested and ready - "
     "waiting on your go-ahead.", {}, False),
]

CHATS = [
    # (minutes ago, chat, key, is_group, sender, text)
    (240, "Ravi Kumar", "919812345678@s.whatsapp.net", False, "Ravi Kumar",
     "The payment gateway is not working, customers are calling the store non stop"),
    (95, "Ravi Kumar", "919812345678@s.whatsapp.net", False, "Ravi Kumar", "Can you call me when you are free?"),
    (130, "Leadership", "120363041234567890@g.us", True, "Meera Iyer", "Board deck draft 3 is out, comments by tomorrow please"),
    (120, "Leadership", "120363041234567890@g.us", True, "Karan Shah", "Acme is signed!! 🎉"),
    (60, "Sunita (home)", "919800055566@s.whatsapp.net", False, "Sunita (home)", "Dinner at 8 with the Mehtas, don't be late"),
    (25, "Daniel Park", "14155550123@s.whatsapp.net", False, "Daniel Park", "Thanks for the intro yesterday, really appreciate it"),
]


def seed(app: "Felix") -> dict[str, int]:
    """Fill ``app`` with sample data. Safe to call once on an empty store."""
    now = app.clock.now()
    app.config.update({"owner": {"name": app.owner_name() or "Vikram", "email": DEMO_ACCOUNT["email"],
                                 "phone": "+919800000001"}})
    account = Account.from_config(DEMO_ACCOUNT)

    for person in (("Anita Rao", "anita.rao@bigretail.example", "+919811122233", True),
                   ("Meera Iyer", "meera@northwind.example", "+919800011122", False),
                   ("Ravi Kumar", "ravi@northwind.example", "+919812345678", False),
                   ("Priya Nair", "priya@northwind.example", "+919800077788", False),
                   ("Karan Shah", "karan@northwind.example", "+919877766655", False)):
        app.contacts.upsert(person[0], [person[1]], [person[2]], source="demo", vip=person[3])

    for uid, (hours, sender, subject, body, headers, read) in enumerate(EMAILS, start=1):
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = sender, DEMO_ACCOUNT["email"], subject
        message["Date"] = format_datetime(now - timedelta(hours=hours))
        message["Message-ID"] = make_msgid(domain="northwind.example")
        for key, value in headers.items():
            message[key] = value
        message.set_content(body)
        raw = message.as_bytes()
        app.mail._store_message(account, "INBOX", 1, uid, {"\\seen"} if read else set(), len(raw), raw, "in")

    for minutes, chat, key, group, sender, text in CHATS:
        app.whatsapp.receive("whatsapp", key, chat, group, sender, text, (now - timedelta(minutes=minutes)).timestamp(),
                             "demo", can_reply=True)

    def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
        return (now + timedelta(days=day_offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)

    for title, start, minutes, where in (
            ("Leadership sync", at(0, 11), 45, "Board room"), ("Lunch with Lumen Ventures", at(0, 13), 60, "Trishna"),
            ("Customer call: BigRetail", at(0, 15, 30), 30, "Zoom"), ("1:1 with Meera", at(0, 16), 30, ""),
            ("Board prep", at(1, 10), 90, "Board room"), ("Investor update call", at(1, 15), 45, "Zoom"),
            ("Flight to Mumbai", at(6, 7, 30), 120, "BLR T2")):
        app.calendar.create(title, start, start + timedelta(minutes=minutes), location=where)
    app.calendar.create("Quarterly review", at(0, 15), at(0, 16), location="Board room")       # a deliberate clash

    app.tasks.add("Approve payroll run", due=at(-1, 18), priority=1)
    app.tasks.add("Sign the office lease renewal", due=at(0, 17))
    app.tasks.add("Review board deck revenue slide", due=at(1, 12), priority=1)
    app.tasks.add("Call the bank about the credit line", due=at(2, 11))
    done = app.tasks.add("Send offer letter to the new CFO candidate")
    app.tasks.complete(done["id"])
    app.memory.remember("The board meets on the first Monday of every quarter.")
    app.memory.remember("Priya is my chief of staff; route scheduling questions to her.")
    app.memory.remember("No meetings before 9:30 am.")

    # One draft waiting for the owner, as Felix would produce it.
    budget = app.store.one("SELECT id FROM emails WHERE subject = 'Q4 budget'")
    if budget:
        app.tools.execute("email_reply", {
            "id": budget["id"], "body": "Hi Meera,\n\nApproved - please go ahead and release the hiring budget.\n\nThanks"},
            ToolContext(app, thread="main", source="demo", actor="felix"))
    brief = app.briefing.run("morning", deliver=False)
    return {"emails": len(EMAILS), "messages": len(CHATS), "brief_chars": len(brief["text"])}
