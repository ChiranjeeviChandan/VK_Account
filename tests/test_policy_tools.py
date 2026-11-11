"""The safety layer: validation, approvals, and who is allowed to approve."""

import pytest

from felix.tools import DANGER, OUTBOUND, Tool, ToolError, redact, validate

SCHEMA = {
    "type": "object",
    "properties": {
        "to": {"type": "string"},
        "count": {"type": "integer", "minimum": 1, "maximum": 5},
        "urgent": {"type": "boolean"},
        "mode": {"type": "string", "enum": ["fast", "slow"]},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["to"],
    "additionalProperties": False,
}


def test_validate_accepts_and_coerces():
    clean, problems = validate({"to": "a", "count": "3", "urgent": "yes", "mode": "FAST", "tags": "x"}, SCHEMA)
    assert problems == []
    assert clean == {"to": "a", "count": 3, "urgent": True, "mode": "fast", "tags": ["x"]}


def test_validate_reports_every_problem():
    _, problems = validate({"count": 9, "mood": "x", "mode": "warp"}, SCHEMA)
    text = "; ".join(problems)
    assert "missing required field to" in text
    assert "at most 5" in text
    assert "unknown field 'mood'" in text and "did you mean 'mode'" in text
    assert "must be one of fast, slow" in text


def test_validate_null_optional_is_dropped():
    clean, problems = validate({"to": "a", "count": None}, SCHEMA)
    assert problems == [] and clean == {"to": "a"}


def test_validate_rejects_bool_for_integer():
    _, problems = validate({"to": "a", "count": True}, SCHEMA)
    assert problems


def test_redact_hides_secrets_and_clips():
    out = redact({"password": "hunter2", "body": "x" * 1000, "nested": {"api_key": "k"}})
    assert out["password"] == "***" and out["nested"]["api_key"] == "***"
    assert len(out["body"]) <= 400


@pytest.fixture
def sent(app):
    """Registers a fake outbound tool and a fake dangerous tool."""
    log: list[dict] = []
    app.tools.add(Tool(
        name="fake_send", description="send", risk=OUTBOUND, channel="email",
        schema={"type": "object", "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
                "required": ["to", "body"]},
        handler=lambda args, ctx: log.append(args) or {"message": f"Sent to {args['to']}."},
        preview=lambda args, ctx: f"Send email\nTo: {args['to']}\n---\n{args['body']}",
        recipients=lambda args, ctx: [args["to"]],
    ))
    app.tools.add(Tool(
        name="fake_shell", description="run", risk=DANGER, channel="shell",
        schema={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
        handler=lambda args, ctx: log.append(args) or "ran",
    ))
    return log


def test_outbound_waits_for_approval(app, ctx, sent):
    result = app.tools.execute("fake_send", {"to": "ravi@corp.com", "body": "hi"}, ctx)
    assert result.ok and result.pending and sent == []
    assert "awaiting_owner_approval" in result.for_model()
    assert "ravi@corp.com" in result.pending["preview"]
    # the identical call again must not create a second approval
    again = app.tools.execute("fake_send", {"to": "ravi@corp.com", "body": "hi"}, ctx)
    assert again.pending["id"] == result.pending["id"]
    assert len(app.policy.pending()) == 1


def test_approve_runs_once(app, ctx, sent):
    pending = app.tools.execute("fake_send", {"to": "ravi@corp.com", "body": "hi"}, ctx).pending
    done = app.policy.approve(pending["id"], via="test")
    assert done.ok and sent == [{"to": "ravi@corp.com", "body": "hi"}]
    assert app.policy.get(pending["id"])["status"] == "executed"
    second = app.policy.approve(pending["id"], via="test")
    assert not second.ok and len(sent) == 1


def test_owner_can_edit_before_sending(app, ctx, sent):
    pending = app.tools.execute("fake_send", {"to": "ravi@corp.com", "body": "draft"}, ctx).pending
    app.policy.approve(pending["id"], via="test", args={"to": "ravi@corp.com", "body": "final"})
    assert sent == [{"to": "ravi@corp.com", "body": "final"}]


def test_deny(app, ctx, sent):
    pending = app.tools.execute("fake_send", {"to": "x@y.com", "body": "hi"}, ctx).pending
    assert app.policy.deny(pending["id"], via="test").ok
    assert sent == [] and app.policy.pending() == []
    assert not app.policy.approve(pending["id"], via="test").ok


def test_expiry(app, ctx, sent):
    pending = app.tools.execute("fake_send", {"to": "x@y.com", "body": "hi"}, ctx).pending
    app.clock.advance(hours=13)
    assert app.policy.pending() == []
    assert not app.policy.approve(pending["id"], via="test").ok
    assert sent == []


def test_message_to_self_needs_no_approval(app, ctx, sent):
    result = app.tools.execute("fake_send", {"to": "VK@Example.com", "body": "brief"}, ctx)
    assert result.ok and not result.pending and len(sent) == 1


def test_trusted_recipient_and_auto_policy(app, ctx, sent):
    app.config.set("policy.trusted_recipients", ["priya@corp.com"])
    assert not app.tools.execute("fake_send", {"to": "priya@corp.com", "body": "x"}, ctx).pending
    assert app.tools.execute("fake_send", {"to": "other@corp.com", "body": "x"}, ctx).pending
    app.config.set("policy.autonomy.email", "auto")
    assert not app.tools.execute("fake_send", {"to": "third@corp.com", "body": "x"}, ctx).pending


def test_never_policy_refuses(app, ctx, sent):
    app.config.set("policy.autonomy.email", "never")
    result = app.tools.execute("fake_send", {"to": "a@b.com", "body": "x"}, ctx)
    assert not result.ok and "never" in result.error and sent == []


def test_danger_cannot_be_auto_by_accident(app, ctx, sent):
    app.config.set("policy.autonomy.shell", "auto")
    assert app.tools.execute("fake_shell", {"cmd": "rm -rf /"}, ctx).pending
    app.config.set("policy.allow_unattended_danger", True)
    assert not app.tools.execute("fake_shell", {"cmd": "ls"}, ctx).pending


def test_yes_no_replies_are_handled_before_any_model(app, ctx, sent):
    policy = app.policy
    assert policy.handle_reply("yes", "test", "chat") is None          # nothing waiting -> normal message
    app.tools.execute("fake_send", {"to": "a@b.com", "body": "one"}, ctx)
    assert policy.handle_reply("what's the weather", "test", "chat") is None
    assert policy.handle_reply("yes", "other-thread", "chat") is None   # wrong conversation
    reply = policy.handle_reply("Yes, send it", "test", "chat")
    assert reply == "Sent to a@b.com." and len(sent) == 1


def test_ambiguous_yes_lists_choices(app, ctx, sent):
    first = app.tools.execute("fake_send", {"to": "a@b.com", "body": "one"}, ctx).pending
    second = app.tools.execute("fake_send", {"to": "c@d.com", "body": "two"}, ctx).pending
    reply = app.policy.handle_reply("ok", "test", "chat")
    assert "2 things are waiting" in reply and sent == []
    assert app.policy.handle_reply(f"approve {second['id']}", "test", "chat") == "Sent to c@d.com."
    assert app.policy.handle_reply("no", "test", "chat").startswith("Cancelled")
    assert app.policy.get(first["id"])["status"] == "denied"
    assert [s["to"] for s in sent] == ["c@d.com"]


def test_approve_all(app, ctx, sent):
    app.tools.execute("fake_send", {"to": "a@b.com", "body": "one"}, ctx)
    app.tools.execute("fake_send", {"to": "c@d.com", "body": "two"}, ctx)
    reply = app.policy.handle_reply("approve all", "test", "chat")
    assert reply.count("Sent to") == 2 and len(sent) == 2


def test_dangerous_actions_are_approved_at_the_laptop_only(app, ctx, sent):
    """A lost or borrowed phone must not be able to run commands on the laptop."""
    shell = app.tools.execute("fake_shell", {"cmd": "tar czf backup.tgz ."}, ctx).pending
    for remote in ("phone", "whatsapp"):
        refused = app.policy.approve(shell["id"], via=remote)
        assert not refused.ok and "can only be approved on the laptop" in refused.error
    assert app.policy.get(shell["id"])["status"] == "pending" and sent == []        # still there for the owner
    assert app.policy.handle_reply("yes", "test", "whatsapp") == (
        f"#{shell['id']} failed: #{shell['id']} changes your laptop, so it can only be approved on the laptop itself "
        f"(the dashboard, or: felix approve {shell['id']}).")
    assert app.policy.approve(shell["id"], via="dashboard").ok and sent == [{"cmd": "tar czf backup.tgz ."}]
    refusals = app.store.query("SELECT detail, outcome FROM audit WHERE action = 'approval.refused'")
    assert len(refusals) == 3 and refusals[0]["outcome"] == "laptop only" and refusals[0]["detail"]["via"] == "phone"

    # messages can still be approved from the phone: that is what it is for
    mail = app.tools.execute("fake_send", {"to": "a@b.com", "body": "hello"}, ctx).pending
    assert app.policy.approve(mail["id"], via="phone").ok

    # an action on the phone's own screen can be approved from the phone
    app.tools.add(Tool(name="fake_tap", description="tap", risk=DANGER, channel="phone_ui",
                       schema={"type": "object", "properties": {}}, handler=lambda args, ctx: "tapped"))
    tap = app.tools.execute("fake_tap", {}, ctx).pending
    assert app.policy.approve(tap["id"], via="phone").ok

    # the owner may decide to allow full remote control
    app.config.set("policy.remote_danger_approval", True)
    again = app.tools.execute("fake_shell", {"cmd": "uptime"}, ctx).pending
    assert app.policy.approve(again["id"], via="phone").ok and sent[-1] == {"cmd": "uptime"}


def test_no_approve_tool_exists(app):
    """A model must have no way to grant approval."""
    assert not [n for n in app.tools.names() if "approv" in n or "confirm" in n]


def test_side_effects_are_audited(app, ctx, sent):
    pending = app.tools.execute("fake_send", {"to": "a@b.com", "body": "hello"}, ctx).pending
    app.policy.approve(pending["id"], via="dashboard")
    actions = [(r["actor"], r["action"], r["outcome"]) for r in app.store.query("SELECT * FROM audit ORDER BY id")]
    assert ("felix", "approval.requested", "pending") in actions
    assert ("owner", "approval.approved", "ok") in actions          # who said yes, and from where
    assert ("owner", "fake_send", "ok") in actions
    approved = app.store.one("SELECT detail FROM audit WHERE action = 'approval.approved'")["detail"]
    assert approved == {"id": pending["id"], "tool": "fake_send", "via": "dashboard", "edited": False}


def test_unknown_tool_and_bad_input(app, ctx):
    assert "Unknown tool" in app.tools.execute("nope", {}, ctx).error
    assert "invalid input" in app.tools.execute("task_add", {"titel": "x"}, ctx).error


def test_handler_errors_become_results(app, ctx):
    def boom(args, c):
        raise ToolError("no such thing")

    app.tools.add(Tool("fake_read", "r", {"type": "object", "properties": {}}, boom))
    result = app.tools.execute("fake_read", {}, ctx)
    assert not result.ok and result.error == "no such thing"
