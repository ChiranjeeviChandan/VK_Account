"""What's happening / what's broken, built from scored signals."""

from datetime import timedelta

from felix.insights.actions import extract_actions

HOUR = 3600


def feed(app, kind, ref, hours_ago, text, title="", who="", addr="", **extra):
    ts = app.clock.ts() - hours_ago * HOUR
    return app.pulse.ingest(kind, ref, ts, text, title=title, who=who, who_addr=addr,
                            source=extra.pop("source", kind), **extra)


def busy_day(app):
    app.contacts.upsert("Anita Rao", emails=["anita@bigcustomer.com"], vip=True)
    feed(app, "email", 1, 5, "Checkout is failing for all customers since 9am. We are losing orders. Need a fix urgently!",
         "Payment gateway down", "Ops Desk", "ops@corp.com")
    feed(app, "message", 2, 4, "payment gateway is not working, customers calling non stop", who="Ravi Kumar",
         topic="chat:7", source="whatsapp")
    feed(app, "email", 3, 3, "This is unacceptable. Third follow-up and still no response. We will cancel our contract.",
         "Extremely disappointed", "Anita Rao", "anita@bigcustomer.com")
    feed(app, "email", 4, 6, "Your invoice is 45 days overdue. Final reminder.", "Invoice 4471 overdue",
         "Vendor AR", "ar@vendor.com")
    feed(app, "email", 5, 2, "Great news - the contract is signed. Congratulations team!", "We won the Acme deal!",
         "Sales", "sales@corp.com")
    feed(app, "email", 6, 1, "Can you approve the Q4 budget by Friday?", "Q4 budget", "Meera Iyer", "meera@corp.com")
    feed(app, "email", 7, 2, "LAST CHANCE 70% off!!! Shop now. Unsubscribe.", "Mega sale", "Shop", "offers@shop.com")
    feed(app, "email", 8, 7, "Attaching the notes from the review. Next sync Thursday.", "Meeting notes", "EA", "ea@corp.com")


def test_report_ranks_what_is_broken(app):
    busy_day(app)
    report = app.pulse.report()
    titles = [b["title"] for b in report["broken"]]
    # the gateway outage merges its email and WhatsApp reports into one issue
    gateway = next(b for b in report["broken"] if "gateway" in b["title"].lower())
    assert gateway["count"] == 2 and set(gateway["sources"]) == {"email", "whatsapp"}
    assert "incident" in gateway["categories"]
    # the VIP threatening to leave and the outage outrank an overdue invoice
    assert titles.index("Invoice 4471 overdue") == len(titles) - 1
    assert {"Extremely disappointed", "Payment gateway down"} == set(titles[:2]) or gateway["title"] in titles[:2]
    assert report["mood"]["label"] == "tense"
    assert report["counts"]["filtered_bulk"] == 1
    assert not any("sale" in b["title"].lower() for b in report["broken"] + report["happening"])
    assert [w["title"] for w in report["wins"]] == ["We won the Acme deal!"]
    assert "Meeting notes" in [h["title"] for h in report["happening"]]


def test_vip_and_waiting_on_you(app):
    busy_day(app)
    waiting = app.pulse.report()["waiting_on_you"]
    assert waiting[0]["who"] == "Anita Rao" and waiting[0]["vip"]
    assert "Meera Iyer" in [w["who"] for w in waiting]
    assert "Shop" not in [w["who"] for w in waiting]          # marketing never needs a reply
    app.pulse.mark_answered("email", [6])
    assert "Meera Iyer" not in [w["who"] for w in app.pulse.report()["waiting_on_you"]]


def test_resolution_moves_issue_to_recovered(app):
    busy_day(app)
    feed(app, "email", 9, 0.5, "The issue has been resolved and checkout is working fine now. Thanks for the patience.",
         "Re: Payment gateway down", "Ops Desk", "ops@corp.com")
    report = app.pulse.report()
    assert not any("gateway" in b["title"].lower() for b in report["broken"])
    assert any("gateway" in r["title"].lower() for r in report["recovered"])


def test_owner_can_dismiss(app):
    busy_day(app)
    report = app.pulse.report()
    invoice = next(b for b in report["broken"] if "Invoice" in b["title"])
    assert app.pulse.dismiss(invoice["items"][0]["signal_id"])
    assert not any("Invoice" in b["title"] for b in app.pulse.report()["broken"])


def test_old_issues_fall_out_of_the_window(app):
    busy_day(app)
    app.clock.advance(hours=30)
    report = app.pulse.report()
    assert report["broken"] == [] and report["counts"]["signals"] == 0
    assert len(app.pulse.report(hours=72)["broken"]) >= 3


def test_tone_worsening_is_noticed(app):
    texts = ["Thanks, looks great!", "Any update on the delivery?", "Still waiting. This delay is frustrating.",
             "This is unacceptable, I am very disappointed."]
    for index, text in enumerate(texts):
        feed(app, "message", 100 + index, 8 - index * 2, text, who="Karan Shah", topic="chat:9", source="whatsapp")
    person = next(p for p in app.pulse.report()["people"] if p["who"] == "Karan Shah")
    assert person["tone"] == "worsening"


def test_mood_trend_compares_with_previous_window(app):
    feed(app, "message", 1, 30, "Great work, thank you!", who="A", topic="chat:1")
    feed(app, "message", 2, 2, "This is terrible, the server is down again", who="B", topic="chat:2")
    report = app.pulse.report()
    assert report["mood"]["trend"] < -0.5


def test_render_reads_like_a_briefing(app):
    busy_day(app)
    text = app.pulse.render(app.pulse.report())
    assert text.startswith("Last 24h: ")
    assert "What's broken:" in text and "customer at risk" in text
    assert "from Anita Rao (VIP) - awaiting your reply" in text
    assert "Also waiting on you (1):" in text and "- Meera Iyer: Q4 budget" in text
    assert text.count("Acme") == 1                      # good news is not repeated
    assert "Good news:" in text and "Acme" in text
    quiet = app.pulse.render({**app.pulse.report(hours=1), "broken": [], "recovered": []})
    assert "Nothing looks broken." in quiet


def test_explain_shows_evidence(app):
    busy_day(app)
    why = app.pulse.explain("email", 3)
    assert "unacceptable" in " ".join(why["evidence"]) and "churn" in why["categories"]


def test_asks_become_suggested_tasks(app):
    feed(app, "email", 1, 1, "Hi VK, could you send the signed term sheet by Friday? Thanks.", "Term sheet",
         "Meera Iyer", "meera@corp.com")
    suggestion = app.tasks.suggestions()[0]
    assert suggestion["title"] == "Send the signed term sheet (asked by Meera Iyer)"
    due = app.clock.from_ts(suggestion["due_ts"])
    assert (due.strftime("%a"), due.hour) == ("Fri", 18)
    # re-ingesting the same email must not duplicate the suggestion
    feed(app, "email", 1, 1, "Hi VK, could you send the signed term sheet by Friday? Thanks.", "Term sheet",
         "Meera Iyer", "meera@corp.com")
    assert len(app.tasks.suggestions()) == 1


def test_extract_actions(app):
    now = app.clock.now()
    items = extract_actions(
        "Hello,\nPlease review the attached contract before 5pm tomorrow. Also, can you call the landlord?\n"
        "> Could you send the old one\n", now)
    assert [i.title for i in items] == ["Review the attached contract", "Call the landlord"]
    assert items[0].due == (now + timedelta(days=1)).replace(hour=17, minute=0)
    assert items[1].due is None
    assert extract_actions("The meeting went well.", now) == []


def test_action_titles_read_like_a_to_do(app):
    now = app.clock.now()

    def titles(text):
        return [item.title for item in extract_actions(text, now)]

    # a weekday inside a noun phrase is not a deadline phrase to cut out
    item = extract_actions("Please review the revenue slide before Thursday's call.", now)[0]
    assert item.title == "Review the revenue slide before Thursday's call"
    assert item.due is not None and item.due.strftime("%a") == "Thu"
    # "waiting on your X" says what the X is for
    assert titles("Backup gateway is tested and ready - waiting on your go-ahead.") == [
        "Give your go-ahead: backup gateway is tested and ready"]
    assert titles("We need your approval on the vendor contract by tomorrow.") == ["Give your approval on the vendor contract"]
    assert titles("Still waiting for your reply.") == ["Reply"]
    assert titles("We are waiting on your reply about the renewal terms.") == ["Reply about the renewal terms"]
    # plain instructions count; a status update about someone else does not
    assert titles("Call me when free") == ["Call me when free"]
    assert titles("Send me the deck by Friday") == ["Send me the deck"]
    assert titles("Waiting on your team to finish the audit.") == []


def test_pulse_tool(app, ctx):
    busy_day(app)
    result = app.tools.execute("pulse_report", {"hours": 24}, ctx)
    assert result.ok and "What's broken:" in result.data["summary_text"]
    assert app.tools.execute("pulse_explain", {"kind": "email", "id": 3}, ctx).data["severity"] > 0.6
