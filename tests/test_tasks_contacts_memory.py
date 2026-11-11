from datetime import timedelta

import pytest

from felix.contacts import norm_phone, phone_key
from felix.tools import ToolError


def run(app, ctx, tool, **args):
    result = app.tools.execute(tool, args, ctx)
    assert result.ok, result.error
    return result.data


# -- tasks -------------------------------------------------------------------

def test_task_lifecycle(app, ctx):
    added = run(app, ctx, "task_add", title="Send Q3 deck to Meera", due="tomorrow 5pm", priority="high")
    assert added["task"]["due"].startswith("2026-11-12T17:00")
    assert "due tomorrow 5 PM" in added["message"] and "[high]" in added["message"]
    run(app, ctx, "task_add", title="Call the bank")
    listing = run(app, ctx, "task_list")
    assert [t["title"] for t in listing["tasks"]] == ["Send Q3 deck to Meera", "Call the bank"]
    assert run(app, ctx, "task_complete", task="q3 deck")["message"] == "Done: Send Q3 deck to Meera"
    assert [t["title"] for t in run(app, ctx, "task_list")["tasks"]] == ["Call the bank"]
    run(app, ctx, "task_delete", task="2")
    assert run(app, ctx, "task_list")["tasks"] == []
    assert len(run(app, ctx, "task_list", status="all")["tasks"]) == 2


def test_task_time_errors_are_explained(app, ctx):
    result = app.tools.execute("task_add", {"title": "x", "due": "blursday"}, ctx)
    assert not result.ok and "could not understand" in result.error


def test_task_find_ambiguity(app, ctx):
    app.tasks.add("Review hiring plan")
    app.tasks.add("Review pricing plan")
    with pytest.raises(ToolError, match="More than one task"):
        app.tasks.find("review")
    assert app.tasks.find("pricing")["title"] == "Review pricing plan"
    assert app.tasks.find("#1")["title"] == "Review hiring plan"
    assert app.tasks.find("reveiw hiring plan")["title"] == "Review hiring plan"   # typo tolerated


def test_agenda_buckets_and_reminders(app):
    now = app.clock.now()
    app.tasks.add("overdue", due=now - timedelta(days=2))
    app.tasks.add("today", due=now.replace(hour=18))
    app.tasks.add("later", due=now + timedelta(days=3))
    app.tasks.add("someday")
    app.tasks.add("nudge", remind=now + timedelta(minutes=10))
    agenda = app.tasks.agenda()
    assert [t["title"] for t in agenda["overdue"]] == ["overdue"]
    assert [t["title"] for t in agenda["today"]] == ["today"]
    assert [t["title"] for t in agenda["upcoming"]] == ["later"]
    assert {t["title"] for t in agenda["someday"]} == {"someday", "nudge"}
    assert app.tasks.due_reminders() == []
    app.clock.advance(minutes=11)
    assert [t["title"] for t in app.tasks.due_reminders()] == ["nudge"]


def test_suggested_tasks_stay_off_the_list_until_accepted(app, ctx):
    task = app.tasks.add("Reply to Ravi about pricing", origin="email", origin_ref="12", suggested=True)
    # the same extraction twice must not duplicate
    assert app.tasks.add("Reply to Ravi about pricing", origin="email", origin_ref="12", suggested=True)["id"] == task["id"]
    assert run(app, ctx, "task_list")["tasks"] == []
    assert len(run(app, ctx, "task_list", include_suggestions=True)["tasks"]) == 1
    run(app, ctx, "task_update", task=str(task["id"]), accept=True)
    assert len(run(app, ctx, "task_list")["tasks"]) == 1


# -- contacts ----------------------------------------------------------------

def test_phone_normalisation():
    assert norm_phone("+91 98123 45678") == "+919812345678"
    assert norm_phone("98123-45678") == "9812345678"
    assert norm_phone("12") == ""
    assert phone_key("+91 98123 45678") == phone_key("09812345678") == "9812345678"


def test_contacts_merge_by_address(app):
    a = app.contacts.upsert("ravi@corp.com", emails=["ravi@corp.com"], source="mail")
    b = app.contacts.upsert("Ravi Kumar", emails=["Ravi@Corp.com"], phones=["+91 98123 45678"], source="phone")
    assert a == b
    row = app.contacts.get(a)
    assert row["name"] == "Ravi Kumar" and row["phones"] == ["+919812345678"]
    # the same number written differently is the same person
    assert app.contacts.upsert("R. Kumar", phones=["098123 45678"]) == a
    assert app.contacts.name_for("+919812345678") == "Ravi Kumar"


def test_resolve_ranking_and_ambiguity(app):
    app.contacts.upsert("Ravi Kumar", emails=["ravi.k@corp.com"], phones=["+919812345678"])
    app.contacts.upsert("Ravi Shankar", emails=["rs@other.com"])
    app.contacts.upsert("Priya Nair", emails=["priya@corp.com"], phones=["+919800011122"])
    assert app.contacts.address_of("priya", "email") == ("priya@corp.com", "Priya Nair")
    assert app.contacts.address_of("Ravi Kumar", "phone") == ("+919812345678", "Ravi Kumar")
    with pytest.raises(ToolError, match="more than one person"):
        app.contacts.pick("ravi", "email")
    # only one Ravi has a phone number, so asking for a phone is unambiguous
    assert app.contacts.pick("ravi", "phone")["name"] == "Ravi Kumar"
    with pytest.raises(ToolError, match="don't know a contact"):
        app.contacts.pick("zoltan", "email")
    # a literal address always resolves, even for a stranger
    assert app.contacts.address_of("new@person.io", "email") == ("new@person.io", "new@person.io")
    assert app.contacts.pick("priyaa nair", "email")["name"] == "Priya Nair"      # typo tolerated


def test_vip_detection(app):
    app.contacts.upsert("Anita Rao", emails=["anita@board.org"], vip=True)
    app.config.set("owner.vips", ["bigcustomer.com", "+91 98111 22233"])
    assert app.contacts.is_vip("anita@board.org")
    assert app.contacts.is_vip("Anita Rao")
    assert app.contacts.is_vip("cfo@bigcustomer.com")
    assert app.contacts.is_vip("09811122233")
    assert not app.contacts.is_vip("random@nowhere.com", "Some One")


def test_contact_tools(app, ctx):
    run(app, ctx, "contact_save", name="Meera Iyer", email="meera@corp.com", vip=True)
    found = run(app, ctx, "contact_find", query="meera")["contacts"]
    assert found[0]["name"] == "Meera Iyer" and found[0]["vip"] is True
    bad = app.tools.execute("contact_save", {"name": "X", "email": "not-an-email"}, ctx)
    assert not bad.ok and "not a valid email" in bad.error


# -- memory + search -----------------------------------------------------------

def test_memory_roundtrip(app, ctx):
    saved = run(app, ctx, "memory_save", text="The board meets on the first Monday of each month")
    run(app, ctx, "memory_save", text="Never book meetings before 9am")
    assert run(app, ctx, "memory_search", query="board meets")["notes"][0]["id"] == saved["id"]
    assert "Never book meetings before 9am" in app.memory.context_block()
    run(app, ctx, "memory_delete", id=saved["id"])
    assert run(app, ctx, "memory_search", query="board meets")["notes"] == []
    assert not app.tools.execute("memory_delete", {"id": 999}, ctx).ok


def test_search_everything_spans_kinds(app, ctx):
    app.tasks.add("Renew the office lease")
    app.memory.remember("Lease renewal contact is Mr Shah")
    hits = run(app, ctx, "search_everything", query="lease")["results"]
    assert {h["kind"] for h in hits} == {"task", "note"}
    only_tasks = run(app, ctx, "search_everything", query="lease", kinds=["task"])["results"]
    assert [h["kind"] for h in only_tasks] == ["task"]
