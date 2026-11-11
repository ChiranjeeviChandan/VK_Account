"""Email end to end against the in-repo IMAP + SMTP servers (real sockets)."""

import email
import json
import time
from datetime import timedelta

import pytest

from felix.connectors import mail as mail_module
from felix.connectors.mail import parse_message, preset_for
from felix.tools import ToolContext

from .fakes.mailworld import MailWorld


@pytest.fixture
def world():
    w = MailWorld()
    w.add_user("vk", "secret", "vk@example.com")
    w.add_user("ravi", "secret", "ravi@corp.com")
    w.start()
    yield w
    w.stop()


@pytest.fixture
def mapp(app, net, world):
    """An app with one mailbox on the fake servers, online."""
    net.up = True
    app.mail.add_account({
        "id": "work", "email": "vk@example.com", "username": "vk",
        "imap_host": "127.0.0.1", "imap_port": world.imap_port, "imap_security": "none",
        "smtp_host": "127.0.0.1", "smtp_port": world.smtp_port, "smtp_security": "none",
    }, password="secret")
    return app


def run(app, tool, ctx=None, **args):
    result = app.tools.execute(tool, args, ctx or ToolContext(app, thread="t", source="test"))
    assert result.ok, result.error
    return result


def approve(app, result):
    assert result.pending, "expected an approval request"
    done = app.policy.approve(result.pending["id"], via="test")
    assert done.ok, done.error
    return done.data


def last_delivery(world):
    return email.message_from_bytes(world.delivered[-1]["raw"], policy=email.policy.default)


# -- sync ------------------------------------------------------------------------

def test_sync_parses_and_scores(mapp, world):
    now = mapp.clock.now()
    world.deliver("vk@example.com", "Ravi Kumar <ravi@corp.com>", "Payment gateway down",
                  "Checkout is failing for all customers. Need a fix urgently!", when=now - timedelta(hours=2))
    world.deliver("vk@example.com", "Deals <offers@shop.com>", "70% off today",
                  "Shop now", when=now - timedelta(hours=1), headers={"List-Unsubscribe": "<mailto:u@shop.com>"})
    world.deliver("vk@example.com", "Meera <meera@corp.com>", "Deck", "",
                  html="<html><body><p>Hi VK,</p><p>Deck <b>attached</b>.</p></body></html>",
                  when=now - timedelta(minutes=30), attachment=("deck.pdf", b"%PDF-1.4 fake"))
    summary = mapp.mail.sync()
    assert summary == {"new": 3, "updated": 0, "removed": 0, "errors": []}

    rows = mapp.mail.list(include_bulk=True)
    assert [r["subject"] for r in rows] == ["Deck", "70% off today", "Payment gateway down"]
    deck, promo, outage = rows
    assert deck["body"] == "Hi VK,\n\nDeck attached." and deck["attachments"][0]["name"] == "deck.pdf"
    assert promo["is_bulk"] == 1 and outage["is_bulk"] == 0
    assert (outage["from_name"], outage["from_addr"]) == ("Ravi Kumar", "ravi@corp.com")
    assert [r["subject"] for r in mapp.mail.list()] == ["Deck", "Payment gateway down"]   # promo hidden

    # the sender became a contact, the outage became a flagged signal; the promo did neither
    assert mapp.contacts.by_address("ravi@corp.com")["name"] == "Ravi Kumar"
    assert mapp.contacts.by_address("offers@shop.com") is None
    report = mapp.pulse.report()
    assert [b["title"] for b in report["broken"]] == ["Payment gateway down"]
    assert report["counts"]["filtered_bulk"] == 1

    # a second sync asks only for newer UIDs and adds nothing
    world.imap_commands.clear()
    assert mapp.mail.sync()["new"] == 0
    assert "UID SEARCH UID 4:*" in world.imap_commands
    assert not any("BODY.PEEK[]" in c for c in world.imap_commands)


def test_initial_sync_respects_the_window(mapp, world):
    now = mapp.clock.now()
    world.deliver("vk@example.com", "old@corp.com", "Ancient", "long ago", when=now - timedelta(days=60))
    world.deliver("vk@example.com", "new@corp.com", "Recent", "just now", when=now - timedelta(days=1))
    assert mapp.mail.sync()["new"] == 1
    assert [r["subject"] for r in mapp.mail.list()] == ["Recent"]


def test_flag_changes_and_deletions_are_mirrored(mapp, world):
    world.deliver("vk@example.com", "a@corp.com", "One", "first", when=mapp.clock.now())
    world.deliver("vk@example.com", "b@corp.com", "Two", "second", when=mapp.clock.now())
    mapp.mail.sync()
    inbox = world.users["vk"].mailboxes["INBOX"]
    inbox.messages[0].flags.add("\\Seen")          # read on another device
    inbox.messages.pop(1)                           # deleted on another device
    summary = mapp.mail.sync()
    assert (summary["updated"], summary["removed"]) == (1, 1)
    rows = mapp.mail.list()
    assert [(r["subject"], r["is_read"]) for r in rows] == [("One", 1)]
    assert mapp.store.search("second", kinds=["email"]) == []


def test_uidvalidity_change_resets_the_cache(mapp, world):
    world.deliver("vk@example.com", "a@corp.com", "Before", "x", when=mapp.clock.now())
    mapp.mail.sync()
    inbox = world.users["vk"].mailboxes["INBOX"]
    inbox.uidvalidity, inbox.uidnext, inbox.messages = 99, 1, []
    world.deliver("vk@example.com", "a@corp.com", "After", "y", when=mapp.clock.now())
    mapp.mail.sync()
    assert [r["subject"] for r in mapp.mail.list()] == ["After"]


def test_oversized_messages_are_fetched_in_part(mapp, world, monkeypatch):
    monkeypatch.setattr(mail_module, "MAX_FULL_FETCH", 500)
    world.deliver("vk@example.com", "big@corp.com", "Huge report", "start " + "x" * 5000, when=mapp.clock.now())
    assert mapp.mail.sync()["new"] == 1
    row = mapp.mail.list()[0]
    assert row["subject"] == "Huge report" and row["body"].startswith("start xxx")
    assert any("BODY.PEEK[HEADER]" in c for c in world.imap_commands)


def test_bad_password_is_reported_plainly(mapp):
    mapp.secrets.set("mail:work:password", "wrong")
    summary = mapp.mail.sync()
    assert "refused the sign-in" in summary["errors"][0]
    check = mapp.mail.check(mapp.mail.account())
    assert not check["ok"] and "refused" in check["imap"] and "refused" in check["smtp"]


def test_sync_offline_does_nothing(mapp, net, world):
    net.up = False
    assert mapp.mail.sync()["errors"] == ["offline"] and world.imap_commands == []


def test_oauth_sign_in_is_used_for_imap_and_smtp(app, net, world):
    net.up = True
    world.tokens["tok-123"] = "vk"
    app.mail.add_account({
        "id": "g", "email": "vk@example.com", "username": "vk", "auth": "oauth_google",
        "imap_host": "127.0.0.1", "imap_port": world.imap_port, "imap_security": "none",
        "smtp_host": "127.0.0.1", "smtp_port": world.smtp_port, "smtp_security": "none"})
    app.secrets.set("oauth:google:vk@example.com", json.dumps(
        {"access_token": "tok-123", "refresh_token": "r", "expires_at": time.time() + 3600}))
    assert app.mail.check(app.mail.account())["ok"]
    app.secrets.set("oauth:google:vk@example.com", json.dumps(
        {"access_token": "stale", "refresh_token": "r", "expires_at": time.time() + 3600}))
    assert "refused the sign-in" in app.mail.check(app.mail.account())["imap"]


# -- sending -----------------------------------------------------------------------

def test_send_waits_for_approval_then_delivers(mapp, world):
    result = run(mapp, "email_send", to=["ravi@corp.com"], subject="Board deck", body="Please find the deck attached.")
    assert world.delivered == []
    assert result.pending["preview"] == (
        "Send email\nTo: ravi@corp.com\nSubject: Board deck\n---\nPlease find the deck attached.")
    data = approve(mapp, result)
    assert data["status"] == "sent" and data["message"] == "Sent to ravi@corp.com."
    sent = last_delivery(world)
    assert sent["From"] == "VK <vk@example.com>" and sent["To"] == "ravi@corp.com"
    assert sent["Subject"] == "Board deck" and "Please find the deck attached." in sent.get_content()
    # the mail server is not told the laptop's name
    assert world.greetings and set(world.greetings) == {"[127.0.0.1]"}
    # it reached Ravi's inbox and a copy was filed in my Sent folder
    assert len(world.users["ravi"].mailboxes["INBOX"].messages) == 1
    filed = world.users["vk"].mailboxes["Sent Items"].messages
    assert len(filed) == 1 and "\\Seen" in filed[0].flags


def test_recipient_names_resolve_through_contacts(mapp, world):
    mapp.contacts.upsert("Ravi Kumar", emails=["ravi@corp.com"])
    mapp.contacts.upsert("Ravi Shankar", emails=["rs@other.com"])
    result = run(mapp, "email_send", to=["Ravi Kumar"], subject="Hi", body="Hello")
    assert "To: ravi@corp.com" in result.pending["preview"]
    ambiguous = mapp.tools.execute("email_send", {"to": ["Ravi"], "subject": "Hi", "body": "x"},
                                   ToolContext(mapp, thread="t"))
    assert not ambiguous.ok and "more than one person" in ambiguous.error


def test_what_is_approved_is_what_is_sent(mapp, world):
    """The address shown in the preview is the address the mail goes to, whatever happens in between."""
    mapp.contacts.upsert("Ravi", emails=["ravi@corp.com"])
    result = run(mapp, "email_send", to=["Ravi"], subject="Board deck", body="Attached.")
    assert "To: ravi@corp.com" in result.pending["preview"]
    assert mapp.policy.get(result.pending["id"])["args"]["to"] == ["ravi@corp.com"]       # pinned, not the name
    # between the request and the approval, "Ravi" is made to mean someone else
    mapp.store.delete("contacts", "name = ?", ("Ravi",))
    run(mapp, "contact_save", name="Ravi", email="someone-else@evil.example", vip=True)
    approve(mapp, result)
    assert last_delivery(world)["To"] == "ravi@corp.com"
    assert world.delivered[-1]["to"] == ["ravi@corp.com"]


def test_hidden_copies_are_shown_before_approval(mapp, world):
    result = run(mapp, "email_send", to=["ravi@corp.com"], subject="Numbers", body="See below.",
                 cc=["meera@corp.com"], bcc=["quiet@elsewhere.example"])
    assert result.pending["preview"] == (
        "Send email\nTo: ravi@corp.com\nCc: meera@corp.com\nBcc (hidden copy): quiet@elsewhere.example\n"
        "Subject: Numbers\n---\nSee below.")


def test_an_address_typed_out_is_never_swapped_for_another(mapp, world):
    mapp.contacts.upsert("Meera Iyer", emails=["meera@corp.com", "meera.personal@home.example"])
    result = run(mapp, "email_send", to=["meera.personal@home.example"], subject="Hi", body="Hello")
    assert "To: meera.personal@home.example" in result.pending["preview"]
    assert mapp.contacts.address_of("meera.personal@home.example", "email") == ("meera.personal@home.example", "Meera Iyer")
    assert mapp.contacts.address_of("Meera", "email") == ("meera@corp.com", "Meera Iyer")


def test_look_alike_addresses_are_not_mine_or_trusted(mapp, world):
    """An accent or another country code makes it somebody else's address."""
    def decision(tool, **args):
        return mapp.policy.decide(mapp.tools.get(tool), args, ToolContext(mapp, thread="t")).action

    assert decision("email_send", to=["vk@example.com"], subject="s", body="b") == "allow"        # a note to myself
    assert decision("email_send", to=["VK@Example.COM"], subject="s", body="b") == "allow"
    assert decision("email_send", to=["vk@\u00e9xample.com"], subject="s", body="b") == "ask"       # accented domain
    assert decision("email_send", to=["vk@example.com", "x@other.example"], subject="s", body="b") == "ask"
    mapp.config.set("policy.trusted_recipients", ["assistant@corp.com", "+14155550123"])
    assert decision("email_send", to=["assistant@corp.com"], subject="s", body="b") == "allow"
    assert decision("email_send", to=["assist\u00e4nt@corp.com"], subject="s", body="b") == "ask"
    assert decision("phone_sms", to="+919800000001", text="note to self") == "allow"
    assert decision("phone_sms", to="9800000001", text="note to self") == "allow"                 # saved without the country code
    assert decision("phone_sms", to="+19800000001", text="hello") == "ask"                        # same digits, another country
    assert decision("phone_sms", to="+14155550123", text="hello") == "allow"
    assert decision("phone_sms", to="+914155550123", text="hello") == "ask"


def test_reply_threads_quotes_and_marks_answered(mapp, world):
    original_id = world.deliver(
        "vk@example.com", "Ravi Kumar <ravi@corp.com>", "Q4 budget", "Can you approve the Q4 budget by Friday?",
        when=mapp.clock.now() - timedelta(hours=1), headers={"Cc": "meera@corp.com, vk@example.com"})
    mapp.mail.sync()
    row = mapp.mail.list()[0]
    assert [w["who"] for w in mapp.pulse.waiting_on_owner()] == ["Ravi Kumar"]

    result = run(mapp, "email_reply", id=row["id"], body="Approved. Go ahead.", reply_all=True)
    assert result.pending["preview"].startswith(
        "Reply to email\nTo: ravi@corp.com\nCc: meera@corp.com\nSubject: Re: Q4 budget\n---\nApproved.")
    approve(mapp, result)

    sent = last_delivery(world)
    assert sent["In-Reply-To"] == original_id and original_id in sent["References"]
    assert sent["Subject"] == "Re: Q4 budget"
    text = sent.get_content()
    assert text.startswith("Approved. Go ahead.") and "> Can you approve the Q4 budget by Friday?" in text
    assert "Ravi Kumar wrote:" in text
    assert set(world.delivered[-1]["to"]) == {"ravi@corp.com", "meera@corp.com"}      # never back to myself
    # answered on the server, locally, and no longer waiting on me
    assert "\\Answered" in world.users["vk"].mailboxes["INBOX"].messages[0].flags
    assert mapp.mail.get(row["id"])["is_answered"] == 1
    assert mapp.pulse.waiting_on_owner() == []


def test_sent_mail_is_synced_into_the_thread(mapp, world):
    world.deliver("vk@example.com", "Ravi Kumar <ravi@corp.com>", "Lease", "Please confirm the lease terms.",
                  when=mapp.clock.now() - timedelta(hours=2))
    mapp.mail.sync()
    inbound = mapp.mail.list()[0]
    approve(mapp, run(mapp, "email_reply", id=inbound["id"], body="Confirmed."))
    mapp.mail.sync()
    thread = mapp.mail.thread(inbound["id"])
    assert [(t["direction"], t["subject"]) for t in thread] == [("in", "Lease"), ("out", "Re: Lease")]
    read = run(mapp, "email_read", id=thread[1]["id"]).data
    assert read["sent_by_owner"] and read["thread"][0]["from"] == "Ravi Kumar"


def test_offline_send_is_queued_and_goes_out_on_reconnect(mapp, net, world):
    net.up = False
    data = approve(mapp, run(mapp, "email_send", to=["ravi@corp.com"], subject="Later", body="Sent from a plane."))
    assert data["status"] == "queued" and "you are offline" in data["message"]
    assert world.delivered == [] and len(mapp.outbox.pending()) == 1
    assert mapp.outbox.flush() == {"sent": 0, "failed": 0, "waiting": 1}       # still offline: keeps waiting
    net.up = True
    assert mapp.outbox.flush() == {"sent": 1, "failed": 0, "waiting": 0}
    assert last_delivery(world)["Subject"] == "Later" and mapp.outbox.pending() == []


def test_temporary_smtp_failure_retries_with_backoff(mapp, world):
    world.smtp_fail_next = 1
    data = approve(mapp, run(mapp, "email_send", to=["ravi@corp.com"], subject="Retry", body="x"))
    assert data["status"] == "queued" and world.delivered == []
    assert mapp.outbox.flush()["sent"] == 0          # backoff has not elapsed yet
    mapp.clock.advance(seconds=31)
    assert mapp.outbox.flush()["sent"] == 1 and len(world.delivered) == 1


def test_rejected_recipient_fails_after_retries_and_is_reported(mapp, world):
    events = []
    mapp.bus.on("outbox.failed", lambda topic, data: events.append(data))
    approve(mapp, run(mapp, "email_send", to=["nobody@bounce.invalid"], subject="Bounce", body="x"))
    for _ in range(10):
        mapp.clock.advance(hours=4)
        mapp.outbox.flush()
    item = mapp.outbox.recent()[0]
    assert item["status"] == "failed" and "No such user" in item["last_error"]
    assert events and events[0]["summary"].startswith("Email to nobody@bounce.invalid")


def test_gmail_style_accounts_do_not_file_a_second_sent_copy(mapp, world):
    account = dict(mapp.config.get("mail.accounts")[0], save_sent=False)
    mapp.config.set("mail.accounts", [account])
    approve(mapp, run(mapp, "email_send", to=["ravi@corp.com"], subject="Once", body="x"))
    assert len(world.delivered) == 1 and world.users["vk"].mailboxes["Sent Items"].messages == []


# -- tools ------------------------------------------------------------------------

def test_list_read_mark_archive_tools(mapp, world):
    now = mapp.clock.now()
    world.deliver("vk@example.com", "Ravi Kumar <ravi@corp.com>", "Lease renewal", "The landlord wants an answer.",
                  when=now - timedelta(hours=3))
    world.deliver("vk@example.com", "Meera <meera@corp.com>", "Lunch", "Lunch on Friday?", when=now - timedelta(hours=1),
                  flags={"\\Seen"})
    assert run(mapp, "email_sync").data["new"] == 2

    listing = run(mapp, "email_list").data
    assert [e["subject"] for e in listing["emails"]] == ["Lunch", "Lease renewal"] and listing["unread_total"] == 1
    assert [e["subject"] for e in run(mapp, "email_list", unread_only=True).data["emails"]] == ["Lease renewal"]
    assert [e["subject"] for e in run(mapp, "email_list", sender="meera").data["emails"]] == ["Lunch"]
    assert [e["subject"] for e in run(mapp, "email_list", query="landlord").data["emails"]] == ["Lease renewal"]
    assert run(mapp, "email_list", since="in 1 hour").data["emails"] == []

    lease = listing["emails"][1]
    assert run(mapp, "email_read", id=lease["id"], mark_read=True).data["body"] == "The landlord wants an answer."
    inbox = world.users["vk"].mailboxes["INBOX"]
    assert "\\Seen" in inbox.messages[0].flags
    run(mapp, "email_mark", id=lease["id"], read=False, flagged=True)
    assert inbox.messages[0].flags == {"\\Flagged"}

    assert run(mapp, "email_archive", id=lease["id"]).data["message"] == f"Moved email #{lease['id']} to Archive."
    assert [m.uid for m in inbox.messages] == [2]
    assert len(world.users["vk"].mailboxes["Archive"].messages) == 1
    assert [e["subject"] for e in run(mapp, "email_list").data["emails"]] == ["Lunch"]


def test_archive_falls_back_to_copy_when_move_is_unsupported(mapp, world):
    world.capabilities = "IMAP4rev1 AUTH=PLAIN"
    world.deliver("vk@example.com", "a@corp.com", "Old", "x", when=mapp.clock.now())
    mapp.mail.sync()
    run(mapp, "email_archive", id=mapp.mail.list()[0]["id"], trash=True)
    assert world.users["vk"].mailboxes["INBOX"].messages == []
    assert len(world.users["vk"].mailboxes["Trash"].messages) == 1


def test_privacy_setting_keeps_bodies_off_the_cloud(mapp, world):
    world.deliver("vk@example.com", "a@corp.com", "Secret", "The acquisition price is 40 crore. " * 20,
                  when=mapp.clock.now())
    mapp.mail.sync()
    email_id = mapp.mail.list()[0]["id"]
    mapp.config.set("brain.share_bodies_with_cloud", False)
    cloud = run(mapp, "email_read", ctx=ToolContext(mapp, engine="claude"), id=email_id).data["body"]
    local = run(mapp, "email_read", ctx=ToolContext(mapp, engine="rules"), id=email_id).data["body"]
    assert "withheld from the cloud" in cloud and len(cloud) < 330
    assert len(local) > 600


def test_no_account_gives_a_helpful_note(app):
    result = app.tools.execute("email_list", {}, ToolContext(app))
    assert result.ok and "No email account is connected" in result.data["note"]
    assert "felix connect mail" in app.tools.execute(
        "email_send", {"to": ["a@b.com"], "subject": "s", "body": "b"}, ToolContext(app).approved(1)).error


# -- pure helpers --------------------------------------------------------------------

def test_presets():
    assert preset_for("someone@gmail.com")["imap_host"] == "imap.gmail.com"
    assert preset_for("someone@outlook.com")["smtp_security"] == "starttls"
    assert preset_for("someone@mycompany.io") is None


def test_parse_message_survives_garbage():
    parsed = parse_message(b"\xff\xfe not an email at all \x00")
    assert parsed["subject"] == "" and parsed["from_addr"] == ""
    encoded = (b"From: =?utf-8?b?UsOhdmkgS3Vtw6Fy?= <ravi@corp.com>\r\nSubject: =?utf-8?q?Caf=C3=A9_plan?=\r\n"
               b"Date: Wed, 11 Nov 2026 09:00:00 +0530\r\nAuto-Submitted: auto-replied\r\n\r\nOut of office\r\n")
    parsed = parse_message(encoded)
    assert parsed["from_name"] == "Rávi Kumár" and parsed["subject"] == "Café plan" and parsed["bulk"]
