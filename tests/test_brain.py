"""The brain: routing between Claude, a local model and offline commands.

The Claude engine runs through the real `anthropic` SDK against a scripted
local server; the local engine runs against a scripted Ollama-compatible
server. No test reaches the internet.
"""

import http.server
import json
import threading

import pytest

pytest.importorskip("anthropic")

from felix.brain.claude import _echo  # noqa: E402
from felix.brain.prompts import SYSTEM  # noqa: E402

from .fakes.anthropic_api import FakeClaude, text, thinking, tool_use  # noqa: E402


@pytest.fixture
def claude(app, net):
    fake = FakeClaude()
    net.up = True
    app.secrets.set("anthropic_api_key", "sk-test-key")
    app.config.update({"brain": {"base_url": fake.url, "max_retries": 0, "web_search": True}})
    yield fake
    fake.stop()


def ask(app, message, thread="main"):
    events = []
    result = app.brain.respond(message, thread=thread, source="test", emit=events.append)
    result["events"] = events
    result["streamed"] = "".join(e["text"] for e in events if e["type"] == "delta")
    return result


# -- routing -------------------------------------------------------------------------

def test_offline_uses_the_command_engine_and_never_calls_out(app, claude, net):
    net.up = False
    result = ask(app, "what's on today")
    assert result == {**result, "reply": "Nothing on your calendar today.", "engine": "rules", "online": False}
    assert claude.requests == [] and app.brain.engine_name() == "rules"
    confused = ask(app, "why is the sky blue")
    assert "I only understand direct commands" in confused["reply"] and claude.requests == []


def test_forced_offline_mode_wins_over_a_live_connection(app, claude):
    assert app.brain.engine_name() == "claude"
    assert ask(app, "go offline")["reply"].startswith("Offline mode")
    assert app.brain.engine_name() == "rules"
    ask(app, "explain our pricing strategy")
    assert claude.requests == []
    ask(app, "auto mode")
    assert app.brain.engine_name() == "claude"


def test_instant_commands_skip_the_model_even_when_online(app, claude, monkeypatch):
    from felix.control.laptop import Laptop

    monkeypatch.setattr(Laptop, "_run", lambda self, candidates, **kw: "")
    result = ask(app, "Felix, lock the screen please")
    assert (result["reply"], result["engine"]) == ("Screen locked.", "rules") and claude.requests == []
    assert result["streamed"] == "Screen locked."


def test_no_api_key_falls_back_to_commands(app, net):
    net.up = True
    assert app.brain.engine_name() == "rules"
    assert ask(app, "my tasks")["reply"] == "Your to-do list is empty."


# -- Claude engine ----------------------------------------------------------------------

def test_plain_answer_streams_and_sends_the_expected_request(app, claude):
    claude.reply(thinking(), text("Paris is the capital of France."))
    result = ask(app, "What is the capital of France?")
    assert result["reply"] == "Paris is the capital of France." and result["engine"] == "claude"
    assert result["streamed"] == result["reply"] and result["model"] == "claude-opus-5-5"
    assert len([e for e in result["events"] if e["type"] == "delta"]) > 3          # arrived in pieces

    request, headers = claude.requests[0], claude.headers[0]
    assert request["model"] == "claude-opus-5-5" and request["stream"] is True and request["max_tokens"] == 64000
    assert request["output_config"] == {"effort": "low"}
    assert request["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in headers["anthropic-beta"]
    assert headers["authorization"] == "Bearer sk-test-key"
    assert "thinking" not in request and "temperature" not in request and "tool_choice" not in request
    assert "speed" not in request
    # the system prompt is fixed text with a cache breakpoint; nothing volatile is in it
    assert request["system"] == [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
    assert request["cache_control"] == {"type": "ephemeral"}
    # the changing facts travel with the user's turn instead
    blocks = request["messages"][0]["content"]
    assert blocks[0]["text"].startswith("<felix_context>\nnow: Wednesday 11 November 2026, 10:00 (Asia/Kolkata)")
    assert "owner: VK" in blocks[0]["text"] and blocks[1] == {"type": "text", "text": "What is the capital of France?"}
    # every Felix tool is offered, in a stable order, plus web search
    names = [t["name"] for t in request["tools"]]
    assert names[:-1] == app.tools.names() and names[-1] == "web_search"
    assert request["tools"][-1]["type"] == "web_search_20260209"
    send = next(t for t in request["tools"] if t["name"] == "email_send")
    assert send["input_schema"]["required"] == ["to", "subject", "body"]


def test_tool_loop_runs_tools_and_keeps_the_full_history(app, claude):
    app.tools.execute("calendar_create", {"title": "Board review", "start": "2026-11-11T15:00:00+05:30"},
                      __import__("felix.tools", fromlist=["ToolContext"]).ToolContext(app))
    claude.reply(thinking("sig-A"), text("Checking your calendar."), tool_use("calendar_list", {"range": "today"}))
    claude.reply(text("You have Board review at 3 PM."))
    result = ask(app, "what do I have today?")
    assert result["reply"] == "Checking your calendar.\n\nYou have Board review at 3 PM."
    assert result["streamed"] == result["reply"]
    tools = [(e["name"], e["status"]) for e in result["events"] if e["type"] == "tool"]
    assert tools == [("calendar_list", "start"), ("calendar_list", "end")]

    # the tool result went back to the model
    follow_up = claude.requests[1]["messages"]
    sent_result = follow_up[-1]["content"][0]
    assert sent_result["type"] == "tool_result" and sent_result["tool_use_id"] == "toolu_calendar_list"
    assert "Board review" in json.dumps(sent_result["content"])

    # next turn: the stored history is replayed exactly, thinking block and signature included
    claude.reply(text("At 3 PM."))
    ask(app, "what time again?")
    history = claude.requests[2]["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant", "user"]
    assert history[1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-A"}
    assert history[1]["content"][2]["type"] == "tool_use" and history[2]["content"][0]["type"] == "tool_result"
    assert history[4]["content"][1]["text"] == "what time again?"
    assert history[0] == claude.requests[0]["messages"][0]            # the earlier turn is byte-for-byte unchanged


def test_sending_needs_the_owner_and_yes_is_not_seen_by_the_model(app, claude):
    app.contacts.upsert("Ravi Kumar", emails=["ravi@corp.com"])
    claude.reply(tool_use("email_send", {"to": ["Ravi Kumar"], "subject": "Running late", "body": "Ten minutes behind."}))
    claude.reply(text("I've drafted the email to Ravi. Say yes to send it."))
    result = ask(app, "tell ravi by email I'm ten minutes late")
    assert result["engine"] == "claude" and len(result["approvals"]) == 1
    approval = result["approvals"][0]
    assert approval["tool"] == "email_send" and "To: ravi@corp.com" in approval["preview"]
    assert [e["type"] for e in result["events"] if e["type"] == "approval"] == ["approval"]
    told = json.loads(claude.requests[1]["messages"][-1]["content"][0]["content"])
    assert told["status"] == "awaiting_owner_approval" and "cannot approve" in told["note"]
    assert app.outbox.recent() == []                                   # nothing has been sent

    calls = len(claude.requests)
    decided = ask(app, "yes")
    assert decided["engine"] == "policy" and len(claude.requests) == calls     # handled by code, not the model
    assert app.policy.get(approval["id"])["status"] in ("executed", "failed")

    # the model is told what happened while it was not looking
    claude.reply(text("Noted."))
    ask(app, "thanks, anything else pending?")
    context = claude.requests[-1]["messages"][-1]["content"][0]["text"]
    assert "earlier_in_this_conversation:" in context and "- owner: yes" in context


@pytest.mark.filterwarnings("ignore:Tool 'approve' not found")
def test_model_cannot_approve_its_own_request(app, claude):
    claude.reply(tool_use("laptop_shell", {"command": "rm -rf ~"}), tool_use("approve", {"id": 1}, "toolu_x"))
    claude.reply(text("Waiting for you."))
    result = ask(app, "clean up my home folder")
    assert len(result["approvals"]) == 1 and app.policy.get(1)["status"] == "pending"
    answer = claude.requests[1]["messages"][-1]["content"]
    assert answer[1]["is_error"] is True and "not found" in json.dumps(answer[1]["content"])


def test_tool_errors_are_reported_to_the_model_as_errors(app, claude):
    claude.reply(tool_use("email_read", {"id": 999}))
    claude.reply(text("That email does not exist."))
    ask(app, "read email 999")
    result = claude.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "There is no email #999" in json.dumps(result["content"])


def test_refusal_ends_the_conversation_cleanly(app, claude):
    claude.reply(text("partial"), stop="refusal")
    result = ask(app, "something the model declines")
    assert result["reply"] == "I can't help with that request."
    claude.reply(text("Fresh start."))
    ask(app, "hello again")
    assert len(claude.requests[1]["messages"]) == 1                    # the declined turn was not replayed


def test_cut_off_tool_call_never_runs(app, claude):
    claude.reply(tool_use("laptop_control", {"action": "lock"}), stop="max_tokens")
    result = ask(app, "do a very long thing")
    assert "ran out of room" in result["reply"]
    assert not [e for e in result["events"] if e["type"] == "tool"]


def test_auth_failure_falls_back_with_an_explanation(app, claude):
    claude.fail(401, "authentication_error", "invalid x-api-key")
    result = ask(app, "what's on today")
    assert result["engine"] == "rules" and result["notes"] == [
        "Claude rejected the API key. Set a valid one with: felix connect claude"]
    assert result["reply"].startswith("(Claude rejected the API key.") and "Nothing on your calendar today." in result["reply"]


def test_unreachable_api_falls_back_and_rechecks_the_connection(app, claude, net):
    claude.stop()
    probes = []
    app.net._probe = lambda: probes.append(1) or True
    result = ask(app, "my tasks")
    assert result["engine"] == "rules" and "Cannot reach Claude." in result["reply"] and probes


def test_rejected_history_is_retired_and_retried_fresh(app, claude):
    claude.reply(text("First answer."))
    ask(app, "first question")
    claude.fail(400, "invalid_request_error", "messages.1: thinking block is not valid for this conversation")
    claude.reply(text("Second answer."))
    result = ask(app, "second question")
    assert result["reply"] == "Second answer." and result["engine"] == "claude"
    retry = claude.requests[-1]["messages"]
    assert len(retry) == 1 and "- owner: first question" in retry[0]["content"][0]["text"]     # recap, not replay


def test_conversations_rotate_after_idle_time(app, claude):
    claude.reply(text("One."))
    ask(app, "question one")
    app.clock.advance(hours=4)
    claude.reply(text("Two."))
    ask(app, "question two")
    assert len(claude.requests[1]["messages"]) == 1
    transcript = [(t["role"], t["text"]) for t in app.brain.transcript()]
    assert transcript == [("user", "question one"), ("assistant", "One."), ("user", "question two"), ("assistant", "Two.")]
    assert ask(app, "new chat")["reply"].startswith("Started a fresh conversation.")


def test_threads_are_independent(app, claude):
    claude.reply(text("From the overlay."))
    ask(app, "overlay question", thread="overlay")
    claude.reply(text("From the phone."))
    ask(app, "phone question", thread="phone")
    assert len(claude.requests[1]["messages"]) == 1
    assert [t["text"] for t in app.brain.transcript("phone")] == ["phone question", "From the phone."]


def test_model_settings_shape_the_request(app, claude):
    app.config.update({"brain": {"fast": True, "effort": "high", "threads": False, "web_search": False}})
    claude.reply(text("ok"))
    ask(app, "hello there")
    request = claude.requests[0]
    assert request["speed"] == "fast" and request["output_config"] == {"effort": "high"}
    assert "thread" not in request and "web_search" not in [t["name"] for t in request["tools"]]

    app.config.set("brain.model", "claude-haiku-5-5")           # no server-side refusal fallback, no fast mode
    app.brain.reset()
    claude.reply(text("ok"))
    ask(app, "hello again")
    request = claude.requests[1]
    assert request["model"] == "claude-haiku-5-5" and "fallbacks" not in request and "speed" not in request
    assert "server-side-fallback" not in claude.headers[1].get("anthropic-beta", "")


def test_threads_send_only_new_messages_inside_a_tool_loop(app, claude):
    claude.reply(tool_use("task_list", {}))
    claude.reply(text("Nothing to do."))
    ask(app, "anything on my list?")
    first, second = claude.requests
    assert first["thread"] == {"type": "create"} and "system" in first and "tools" in first
    assert second["thread"]["type"] == "continue" and second["thread"]["previous_message_id"] == "msg_1"
    assert "system" not in second and "tools" not in second
    assert [m["role"] for m in second["messages"]] == ["user"]          # only the tool results


def test_complete_is_a_plain_request(app, claude):
    claude.reply(text("*Good morning*\n• Tight."))
    assert app.brain.claude.complete("Be brief.", "Draft: ...", max_tokens=4000) == "*Good morning*\n• Tight."
    request = claude.requests[0]
    assert request["system"] == "Be brief." and request["max_tokens"] == 4000 and "tools" not in request


def test_echo_drops_declined_model_blocks_but_keeps_the_marker():
    blocks = [{"type": "thinking", "thinking": "", "signature": "a"}, {"type": "text", "text": "partial"},
              {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
              {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"}},
              {"type": "thinking", "thinking": "", "signature": "b"}, {"type": "text", "text": "answer"}]
    kept = _echo(blocks)
    assert [b["type"] for b in kept] == ["text", "fallback", "thinking", "text"] and kept[2]["signature"] == "b"
    untouched = [{"type": "thinking", "thinking": "", "signature": "a"}, {"type": "text", "text": "hi"}]
    assert _echo(untouched) == untouched


# -- local model ---------------------------------------------------------------------------

class FakeOllama:
    def __init__(self):
        self.script, self.requests = [], []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = json.dumps({"models": [{"name": "llama3.1:latest"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                fake.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                step = fake.script.pop(0)
                lines = []
                for word in step.get("content", "").split(" "):
                    if word:
                        lines.append({"message": {"role": "assistant", "content": word + " "}, "done": False})
                last = {"message": {"role": "assistant", "content": ""}, "done": True}
                if step.get("tool_calls"):
                    last["message"]["tool_calls"] = step["tool_calls"]
                data = "".join(json.dumps(line) + "\n" for line in lines + [last]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


@pytest.fixture
def ollama(app):
    fake = FakeOllama()
    app.config.update({"brain": {"local": {"url": fake.url, "model": "llama3.1"}}})
    yield fake
    fake.stop()


def test_local_model_answers_offline_with_tools(app, ollama):
    app.tasks.add("Renew passport")
    ollama.script = [
        {"tool_calls": [{"function": {"name": "task_list", "arguments": {}}}]},
        {"content": "You have one task: renew your passport."},
    ]
    assert app.brain.engine_name() == "local"
    result = ask(app, "what should I be doing?")
    assert result["engine"] == "local" and result["online"] is False
    assert result["reply"] == "You have one task: renew your passport." and result["streamed"].strip() == result["reply"]
    first, second = ollama.requests
    assert first["model"] == "llama3.1" and first["messages"][0]["role"] == "system" and first["stream"] is True
    assert "<felix_context>" in first["messages"][1]["content"]
    assert {t["function"]["name"] for t in first["tools"]} == set(app.tools.names())
    assert second["messages"][-1]["role"] == "tool" and "Renew passport" in second["messages"][-1]["content"]


def test_local_model_is_second_choice_and_takes_over_when_claude_fails(app, claude, ollama):
    assert app.brain.engine_name() == "claude"
    claude.fail(401, "authentication_error", "invalid x-api-key")
    ollama.script = [{"content": "Answering locally."}]
    result = ask(app, "tell me something")
    assert result["engine"] == "local" and result["reply"] == "Answering locally."
    assert result["notes"] == ["Claude rejected the API key. Set a valid one with: felix connect claude"]


def test_local_model_failure_falls_through_to_commands(app, ollama):
    ollama.stop()
    app.brain.local._checked_at, app.brain.local._up = 1e12, True         # believed up, actually gone
    result = ask(app, "my tasks")
    assert result["engine"] == "rules" and "Your to-do list is empty." in result["reply"]
