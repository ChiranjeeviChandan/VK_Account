"""Stand-in for the Node WhatsApp sidecar: same JSON-lines protocol, no WhatsApp.

Extra commands for tests: {"cmd": "emit", "payload": {...}} prints an event,
{"cmd": "crash"} exits abruptly.
"""

import json
import sys

ME = {"jid": "919800000001@s.whatsapp.net", "name": "VK"}


def out(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main():
    out({"event": "state", "state": "qr", "qr": "2@fake-qr-payload"})
    out({"event": "state", "state": "open", "me": ME})
    out({"event": "contacts", "contacts": [{"jid": "919812345678@s.whatsapp.net", "name": "Ravi Kumar"}]})
    counter = 0
    for line in sys.stdin:
        try:
            command = json.loads(line)
        except ValueError:
            continue
        name = command.get("cmd")
        if name == "send":
            counter += 1
            if command.get("to", "").startswith("000"):
                out({"reply": command["id"], "ok": False, "error": "not on WhatsApp"})
                continue
            message_id = f"SENT{counter}"
            out({"reply": command["id"], "ok": True, "id": message_id})
            # WhatsApp echoes our own message back as a from_me event
            out({"event": "message", "id": message_id, "chat": command["to"], "from_me": True,
                 "text": command["text"], "ts": 0})
        elif name == "pairing_code":
            out({"reply": command["id"], "ok": True, "code": "ABCD-1234"})
        elif name == "logout":
            out({"reply": command["id"], "ok": True})
            out({"event": "state", "state": "logged_out"})
        elif name == "emit":
            out(command["payload"])
        elif name == "crash":
            sys.exit(3)
        elif name == "shutdown":
            return


if __name__ == "__main__":
    main()
