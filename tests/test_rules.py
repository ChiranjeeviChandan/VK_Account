"""The offline command engine: what it understands, and what it must not guess."""

import pytest

from felix.brain.rules import Rules, normalise
from felix.tools import ToolContext

from .fakes.simphone import SimPhone


@pytest.fixture
def say(app):
    rules = Rules(app)
    ctx = ToolContext(app, thread="t", source="test", actor="owner", engine="rules")

    def ask(text):
        return rules.respond(text, ctx)

    ask.rules, ask.ctx = rules, ctx
    return ask


def test_normalise_strips_politeness_only():
    assert normalise("Hey Felix, could you please lock the screen?") == "lock the screen?"
    assert normalise("  what's   on today?? ") == "what's on today??"
    assert normalise("I want you to open Excel please") == "open Excel please"
    assert normalise("Please email Ravi saying Thanks.") == "email Ravi saying Thanks."


def test_message_text_is_kept_exactly_but_commands_ignore_punctuation(say, app):
    app.contacts.upsert("Ravi Kumar", emails=["ravi@corp.com"])
    say("email Ravi saying Are we still on for Friday?")
    assert app.policy.pending()[-1]["args"]["body"] == "Are we still on for Friday?"
    assert say("What's on today??") == "Nothing on your calendar today."
    assert say("my tasks, please.") == "Your to-do list is empty."


# -- calendar ----------------------------------------------------------------------

def test_calendar_round_trip(say):
    assert say("what's on today") == "Nothing on your calendar today."
    assert say("schedule a call with Ravi tomorrow at 3pm for 45 minutes") == "Added: Call with Ravi, tomorrow 3 PM–3:45 PM."
    assert say("book board review on friday 10-11am") == "Added: Board review, Fri 13 Nov 10 AM–11 AM."
    assert say("add lunch with investors tomorrow 1pm") == "Added: Lunch with investors, tomorrow 1 PM–1:30 PM."
    assert say("block 14 Nov for the offsite") == "Added: Offsite, Sat 14 Nov, all day."
    assert say("What's on tomorrow?") == (
        "Tomorrow - 2 events:\n• tomorrow 1 PM–1:30 PM  Lunch with investors\n• tomorrow 3 PM–3:45 PM  Call with Ravi")
    assert say("my calendar for friday").startswith("Friday 13 nov - 1 event:")
    assert say("meetings with ravi").endswith("• tomorrow 3 PM–3:45 PM  Call with Ravi")
    assert say("what's next") == "Next: Lunch with investors, tomorrow 1 PM–1:30 PM (in 1 d)."
    assert say("move call with ravi to friday 4pm") == "Updated: Call with Ravi, Fri 13 Nov 4 PM–4:45 PM."
    assert say("cancel lunch with investors") == "Removed: Lunch with investors, tomorrow 1 PM–1:30 PM."
    assert say("cancel yoga class") == 'I can\'t find an event matching "yoga class" in the next few weeks.'
    assert say("schedule a meeting with Meera") == \
        'When should I put "with Meera" on the calendar? Say it again with a day and time.'


def test_free_time(say):
    say("schedule review tomorrow 10am for 2 hours")
    assert say("am I free tomorrow") == (
        "Free for 30 minutes or more:\n• tomorrow 9 AM–10 AM\n• tomorrow 12 PM–6 PM")
    assert say("find a 90 minute slot tomorrow").startswith("Free for 90 minutes or more:\n• tomorrow 12 PM–6 PM")
    assert say("free time this weekend") == "No gap of that length inside working hours in that period."
    # "this week" and "this afternoon" keep their "this"
    assert say("find 45 minutes this week").startswith("Free for 45 minutes or more:\n• today 10 AM–6 PM\n• tomorrow 9 AM–10 AM")
    assert say("am I free this afternoon") == "Free for 30 minutes or more:\n• today 12 PM–6 PM"
    assert say("am I free tomorrow afternoon") == "Free for 30 minutes or more:\n• tomorrow 12 PM–6 PM"


# -- to-do ---------------------------------------------------------------------------

def test_tasks(say, app):
    assert say("my tasks") == "Your to-do list is empty."
    assert say("add task send the Q3 deck to Meera by Friday") == \
        "Added to your list: #1 Send the Q3 deck to Meera (due Fri 13 Nov 6 PM)."
    assert say("remind me to call the bank in 20 minutes") == \
        "Added to your list: #2 Call the bank (due today 10:20 AM). I'll remind you today 10:20 AM."
    assert say("todo: renew passport") == "Added to your list: #3 Renew passport."
    assert say("don't forget to sign the lease tomorrow") == \
        "Added to your list: #4 Sign the lease (due tomorrow 6 PM). I'll remind you tomorrow 9 AM."
    assert say("what is on my list") == say("my tasks") == say("show my list") == say("what's on my to-do list")
    listing = say("what's on my plate")
    assert listing.split("\n")[0] == "Due today:" and "• #2 Call the bank (due today 10:20 AM)" in listing
    assert "No date:\n• #3 Renew passport" in listing
    assert say("done call the bank") == "Done: Call the bank."
    assert say("mark 3 as done") == "Done: Renew passport."
    assert say("delete task sign the lease") == "Removed: Sign the lease."
    assert [t["title"] for t in app.tasks.list()] == ["Send the Q3 deck to Meera"]
    assert say("done walk the dog") == "No open task matches 'walk the dog'."


def test_suggestions_flow(say, app):
    app.tasks.add("Approve the Q4 budget (asked by Meera)", suggested=True, origin="email", origin_ref="5")
    assert "Requests I noticed in your messages:\n• #1 Approve the Q4 budget (asked by Meera)" in say("suggestions")
    assert say("accept 1").startswith("Updated: #1 Approve the Q4 budget")
    assert say("suggestions") == "No suggested to-dos."


# -- brief and pulse -------------------------------------------------------------------

def test_brief_and_pulse_phrasings(say, app):
    from .test_pulse import busy_day

    busy_day(app)
    assert say("brief me").startswith("*Good morning, VK* - Wed 11 Nov")
    assert say("how does my day look").startswith("*Good morning, VK*")
    assert say("evening summary").startswith("*Evening wrap-up, VK*")
    broken = say("what's broken?")
    assert broken.startswith("3 things look broken:") and "Payment gateway down [outage / failure]" in broken
    assert say("any fires").startswith("3 things look broken:")
    assert say("what's happening").startswith("Last 24h: 6 emails, 1 messages; mood tense.")
    waiting = say("who is waiting on me")
    assert waiting.startswith("5 waiting on you:") and "• Anita Rao [VIP] via email" in waiting and "(email #3)" in waiting


def test_quiet_pulse(say):
    assert say("anything broken") == "Nothing looks broken in the last 24 hours."
    assert say("who's waiting") == "Nobody is waiting on you."


# -- email ---------------------------------------------------------------------------

def test_email_commands_build_the_right_drafts(say, app):
    app.contacts.upsert("Meera Iyer", emails=["meera@corp.com"])
    assert say("unread emails") == "No email account is connected yet (felix connect mail)."
    answer = say("email Meera saying The deck is attached. Please review by Friday.")
    assert answer == (
        "Ready - waiting for your go-ahead (#1):\nSend email\nTo: meera@corp.com\n"
        "Subject: The deck is attached\n---\nThe deck is attached. Please review by Friday.\n\n"
        'Reply "yes" to go ahead or "no" to cancel.')
    with_subject = say("send an email to Meera subject Board deck saying Here it is")
    assert "Subject: Board deck\n---\nHere it is" in with_subject
    assert "more than one" not in say("email meera@corp.com, ravi@corp.com saying Hello both")
    assert app.policy.pending()[-1]["args"]["to"] == ["meera@corp.com", "ravi@corp.com"]


def test_email_reply_and_reading(say, app):
    email_id = app.store.insert("emails", {
        "account": "w", "folder": "INBOX", "uid": 1, "uidvalidity": 1, "from_name": "Ravi Kumar",
        "from_addr": "ravi@corp.com", "to_addrs": ["vk@example.com"], "cc_addrs": [], "subject": "Q4 budget",
        "date_ts": app.clock.ts() - 3600, "snippet": "Can you approve?", "body": "Can you approve the Q4 budget?",
        "message_id": "<a@b>", "thread_key": "<a@b>", "direction": "in"})
    assert say(f"read email {email_id}").startswith("From: Ravi Kumar <ravi@corp.com>\nSubject: Q4 budget")
    reply = say(f"reply to {email_id} saying Approved, go ahead.")
    assert "Reply to email\nTo: ravi@corp.com\nSubject: Re: Q4 budget\n---\nApproved, go ahead." in reply
    listing = say("emails from ravi")
    assert listing.startswith("1 email (0 unread in total):\n• #1 Ravi Kumar - Q4 budget")
    assert say(f"mark email {email_id} as unread") == f"Email #{email_id} is now unread."


# -- whatsapp, sms, phone ---------------------------------------------------------------

@pytest.fixture
def phone(app, net):
    net.up = True
    sim = SimPhone(app).start()
    yield sim
    sim.stop()


def test_whatsapp_commands(say, app, phone):
    phone.message("Ravi Kumar", "Are we on for 4pm?", key="com.whatsapp|919812345678@s.whatsapp.net")
    assert say("whatsapp") == "1 unread message:\n• Ravi Kumar (1 new) [needs reply]: Are we on for 4pm?"
    assert say("what did ravi say").startswith("Ravi Kumar:\n• Ravi Kumar (")
    for phrase in ("whatsapp Ravi saying Yes, see you at 4", "tell ravi Yes, see you at 4",
                   "reply to ravi saying Yes, see you at 4", "message Ravi Kumar: Yes, see you at 4"):
        answer = say(phrase)
        assert "Send WhatsApp\nTo: Ravi Kumar (+919812345678)\n---\nYes, see you at 4" in answer, phrase
        for waiting in app.policy.pending():
            app.policy.deny(waiting["id"], via="test")
    assert say("tell zoltan hello there") == (
        "I don't know a contact called 'zoltan' with a phone number. Give me the address or number directly, "
        "or save the contact first.")


def test_reply_to_a_person_uses_the_channel_they_wrote_on(say, app, phone):
    app.contacts.upsert("Anita Rao", ["anita@bigretail.com"], ["+919811122233"])
    email_id = app.store.insert("emails", {
        "account": "w", "folder": "INBOX", "uid": 7, "uidvalidity": 1, "from_name": "Anita Rao",
        "from_addr": "anita@bigretail.com", "to_addrs": ["vk@example.com"], "cc_addrs": [], "subject": "Rollout",
        "date_ts": app.clock.ts() - 600, "snippet": "Still no response", "body": "Still no response on the rollout.",
        "message_id": "<r@b>", "thread_key": "<r@b>", "direction": "in"})
    answer = say("reply to Anita saying We will fix this today.")
    assert "Reply to email\nTo: anita@bigretail.com\nSubject: Re: Rollout\n---\nWe will fix this today." in answer
    assert app.policy.pending()[-1]["args"]["id"] == email_id
    # asked for WhatsApp explicitly
    assert "Send WhatsApp\nTo: Anita Rao (+919811122233)" in say("reply to Anita on whatsapp saying Calling you now")
    # she then writes on WhatsApp, so the next plain "reply to Anita" goes there
    app.clock.advance(minutes=2)
    phone.message("Anita Rao", "Any update?", key="com.whatsapp|919811122233@s.whatsapp.net")
    assert "Send WhatsApp\nTo: Anita Rao" in say("reply to Anita saying An update in 10 minutes")


def test_phone_commands(say, app, phone):
    app.contacts.upsert("Meera Iyer", phones=["+919800011122"])
    assert say("flashlight on") == "Flashlight on." and say("turn off the torch") == "Flashlight off."
    assert say("set alarm for 6:30am") == "Alarm set on your phone for 6:30 AM."
    assert say("wake me up at 5") == "Alarm set on your phone for 5:00 AM."
    assert say("alarm at 5") == "Alarm set on your phone for 5:00 PM."          # 10 am now: the next 5 o'clock
    assert say("timer 10 minutes") == "Timer started on your phone: 10 minutes."
    assert say("find my phone") == "Your phone is ringing loudly for 30 seconds."
    assert say("silence my phone") == "Phone set to silent." and say("dnd on") == "Do not disturb is on."
    assert say("open Maps on my phone") == "Opened Maps on your phone."
    assert say("phone battery") == "Pixel 8: connected, battery 81%, on wifi."
    assert phone.calls[:5] == [("torch", {"on": True}), ("torch", {"on": False}),
                               ("alarm.set", {"hour": 6, "minute": 30, "label": ""}),
                               ("alarm.set", {"hour": 5, "minute": 0, "label": ""}),
                               ("alarm.set", {"hour": 17, "minute": 0, "label": ""})]
    assert say("call Meera").startswith("Ready - waiting for your go-ahead (#1):\nCall\nMeera Iyer - +919800011122")
    assert "Send SMS\nTo: Meera Iyer (+919800011122)\n---\nCall me back" in say("text Meera saying Call me back")
    assert say("set alarm") is None or "What time" in say("set alarm for whenever")


def test_phone_commands_without_a_phone(say):
    assert say("flashlight on") == "No phone is paired with Felix yet. Open the dashboard and choose Pair phone."


# -- laptop ----------------------------------------------------------------------------

def test_laptop_commands(say, app, monkeypatch, tmp_path):
    from felix.control.laptop import Laptop

    ran = []
    monkeypatch.setattr(Laptop, "_run", lambda self, candidates, **kw: ran.append(candidates[0]) or "")
    monkeypatch.setattr(Laptop, "open", lambda self, target: f"Opened {target}.")
    assert say("volume 40") == "Volume set to 40%." and say("set the volume to 15%") == "Volume set to 15%."
    assert say("mute") == "Sound muted." and say("lock the laptop") == "Screen locked."
    assert say("open Excel") == "Opened Excel." and say("launch the app Chrome") == "Opened Chrome."
    assert say("open github.com") == "Opened github.com."
    assert say("sleep").startswith("Ready - waiting for your go-ahead (#1):\nLaptop: sleep")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "Budget 2026.xlsx").write_text("x")
    assert say(f"find file budget in {docs}") == f"1 match(es):\n• {docs / 'Budget 2026.xlsx'}"
    assert say(f"list files in {docs}") == f"{docs}:\n• Budget 2026.xlsx"
    shell = say("run: git status")
    assert "Run shell command\ngit status" in shell and say("$ ls -la").count("Run shell command\nls -la") == 1
    assert say("battery").startswith("Laptop (")


# -- memory, contacts, search, meta -------------------------------------------------------

def test_memory_contacts_search(say, app):
    assert say("remember that the board meets on the first Monday") == "I'll remember that (note #1)."
    assert "the board meets on the first Monday" in say("what do you know about the board")
    assert say("notes") == "What I'm keeping in mind:\n• #1 the board meets on the first Monday"
    assert say("forget 1") == "Forgot note #1."
    assert say("save contact Anita Rao anita@board.org +91 98111 22233") == "Saved Anita Rao."
    assert say("who is anita") == "• Anita Rao · +919811122233 · anita@board.org"
    assert say("mark anita as vip") == "Anita Rao is now a VIP: their messages rank first."
    assert app.contacts.is_vip("anita@board.org")
    app.tasks.add("Renew the office lease")
    assert say("search lease") == "1 result on this laptop:\n• [task #1] Renew the office lease"
    assert say("find unicorns") == 'Nothing stored locally mentions "unicorns".'


def test_meta_commands(say, app):
    assert say("help").startswith("I'm Felix.")
    assert say("go offline").startswith("Offline mode: I won't touch the network")
    assert app.net.mode == "offline" and app.config.get("mode") == "offline"
    assert say("auto mode") == "Automatic mode: I'll use the internet whenever it is there (offline right now)."
    assert say("status").split("\n")[:3] == ["Connection: offline (mode auto).",
                                             "Brain: command engine (no language model).", "Phone: not paired."]
    assert say("pending") == "Nothing is waiting for your approval."
    assert say("what time is it") == "It's 10:00 AM on Wednesday 11 November 2026."
    say("email a@b.com saying hello there")
    assert say("pending").startswith("1 waiting for your approval:\n\n#1  Send email\nTo: a@b.com")
    assert say("deny 1") == "Cancelled #1."


# -- things it must leave to a real model --------------------------------------------------

@pytest.mark.parametrize("text", [
    "why is the sky blue",
    "write a poem about the board",
    "what should I say to Anita about the delay",
    "summarise the email from Ravi and draft a reply",
    "is 221 a prime number",
    "what's on your mind",
    "ok thanks",
    "",
])
def test_unknown_requests_are_not_guessed(say, text):
    assert say(text) is None


def test_unknown_message_points_to_stored_matches(say, app):
    app.memory.remember("The lease renewal is due in March")
    text = say.rules.unknown("what was that thing about the lease renewal", say.ctx)
    assert "I only understand direct commands" in text and "[note #1]" in text and '"help"' in text


def test_fast_only_limits_to_instant_commands(say, app):
    rules, ctx = say.rules, say.ctx
    assert rules.respond("brief me", ctx, fast_only=True) is None
    assert rules.respond("what's on today", ctx, fast_only=True) is None
    assert rules.respond("email Ravi saying hi", ctx, fast_only=True) is None
    assert rules.respond("go offline", ctx, fast_only=True).startswith("Offline mode")
