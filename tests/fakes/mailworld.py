"""A small in-memory mail system for tests: IMAP4rev1 + SMTP over real sockets.

It implements the parts of the protocols Felix uses, strictly enough that
Python's own imaplib and smtplib clients are satisfied. Mail submitted over
SMTP to a known address lands in that user's INBOX, so tests can exercise a
whole conversation. It is a test double - not evidence that any particular
real provider behaves the same way.
"""

from __future__ import annotations

import base64
import re
import socketserver
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

CRLF = b"\r\n"


@dataclass
class Stored:
    uid: int
    raw: bytes
    flags: set[str] = field(default_factory=set)
    internal: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Mailbox:
    name: str
    special: str = ""
    uidvalidity: int = 1
    uidnext: int = 1
    messages: list[Stored] = field(default_factory=list)

    def add(self, raw: bytes, flags: set[str] | None = None, internal: datetime | None = None) -> Stored:
        message = Stored(self.uidnext, raw, set(flags or ()), internal or datetime.now(timezone.utc))
        self.uidnext += 1
        self.messages.append(message)
        return message

    def by_uids(self, spec: str) -> list[Stored]:
        top = max((m.uid for m in self.messages), default=0)
        wanted: list[Stored] = []
        for part in spec.split(","):
            if ":" in part:
                low, high = part.split(":", 1)
                a = top if low == "*" else int(low)
                b = top if high == "*" else int(high)
                a, b = min(a, b), max(a, b)
            else:
                a = b = top if part == "*" else int(part)
            wanted += [m for m in self.messages if a <= m.uid <= b and m not in wanted]
        return wanted


class User:
    def __init__(self, username: str, password: str, address: str) -> None:
        self.username, self.password, self.address = username, password, address.lower()
        self.mailboxes = {
            "INBOX": Mailbox("INBOX"),
            "Sent Items": Mailbox("Sent Items", "\\Sent"),
            "Archive": Mailbox("Archive", "\\Archive"),
            "Trash": Mailbox("Trash", "\\Trash"),
        }


class MailWorld:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.users: dict[str, User] = {}
        self.tokens: dict[str, str] = {}          # bearer token -> username
        self.delivered: list[dict] = []           # every message accepted over SMTP
        self.greetings: list[str] = []            # what each SMTP client called itself
        self.smtp_fail_next = 0                   # reject this many submissions with a temporary error
        self.imap_commands: list[str] = []
        self.capabilities = "IMAP4rev1 AUTH=PLAIN AUTH=XOAUTH2 UIDPLUS MOVE SPECIAL-USE"
        self._servers: list[socketserver.BaseServer] = []
        self.imap_port = 0
        self.smtp_port = 0

    # -- setup -------------------------------------------------------------
    def add_user(self, username: str, password: str, address: str) -> User:
        user = User(username, password, address)
        self.users[username] = user
        return user

    def user_for(self, address: str) -> User | None:
        return next((u for u in self.users.values() if u.address == address.lower()), None)

    def start(self) -> "MailWorld":
        world = self

        class Imap(socketserver.StreamRequestHandler):
            disable_nagle_algorithm = True

            def handle(self) -> None:
                ImapSession(world, self.rfile, self.wfile).run()

        class Smtp(socketserver.StreamRequestHandler):
            disable_nagle_algorithm = True

            def handle(self) -> None:
                SmtpSession(world, self.rfile, self.wfile).run()

        for handler, attr in ((Imap, "imap_port"), (Smtp, "smtp_port")):
            server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = True
            setattr(self, attr, server.server_address[1])
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
            self._servers.append(server)
        return self

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers.clear()

    # -- helpers for tests ---------------------------------------------------
    def deliver(self, to: str, sender: str, subject: str, body: str, when: datetime | None = None,
                headers: dict[str, str] | None = None, folder: str = "INBOX", flags: set[str] | None = None,
                html: str | None = None, attachment: tuple[str, bytes] | None = None) -> str:
        """Drop a message straight into a mailbox, as if it arrived from outside."""
        message = EmailMessage()
        message["From"] = sender
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = format_datetime(when or datetime.now(timezone.utc))
        message["Message-ID"] = make_msgid(domain="test.invalid")
        for key, value in (headers or {}).items():
            message[key] = value
        if html is not None and not body:
            message.set_content(html, subtype="html")
        else:
            message.set_content(body)
            if html is not None:
                message.add_alternative(html, subtype="html")
        if attachment:
            message.add_attachment(attachment[1], maintype="application", subtype="octet-stream", filename=attachment[0])
        user = self.user_for(to)
        assert user is not None, f"no such test user {to}"
        with self.lock:
            user.mailboxes[folder].add(message.as_bytes(), flags, when)
        return message["Message-ID"]


# -- IMAP ------------------------------------------------------------------------

_LITERAL = re.compile(rb"\{(\d+)\+?\}$")


class ImapSession:
    def __init__(self, world: MailWorld, rfile, wfile) -> None:
        self.world, self.rfile, self.wfile = world, rfile, wfile
        self.user: User | None = None
        self.box: Mailbox | None = None
        self.readonly = False

    def send(self, line: str | bytes) -> None:
        data = line.encode("utf-8") if isinstance(line, str) else line
        self.wfile.write(data + CRLF)
        self.wfile.flush()

    def read_command(self) -> tuple[str, list[bytes]] | None:
        """Read one command line, pulling in any literals it announces."""
        line = self.rfile.readline()
        if not line:
            return None
        line = line.rstrip(b"\r\n")
        literals: list[bytes] = []
        while True:
            match = _LITERAL.search(line)
            if not match:
                break
            if not match.group(0).endswith(b"+}"):
                self.send("+ Ready for literal data")
            size = int(match.group(1))
            literals.append(self.rfile.read(size))
            rest = self.rfile.readline().rstrip(b"\r\n")
            line = line[: match.start()] + b"\x00LIT%d\x00" % (len(literals) - 1) + rest
        return line.decode("utf-8", "replace"), literals

    def run(self) -> None:
        self.send("* OK IMAP4rev1 fake server ready")
        while True:
            command = self.read_command()
            if command is None:
                return
            text, literals = command
            tag, _, rest = text.partition(" ")
            verb, _, args = rest.partition(" ")
            verb = verb.upper()
            if verb == "UID":
                sub, _, args = args.partition(" ")
                verb = "UID " + sub.upper()
            self.world.imap_commands.append(f"{verb} {args}".strip())
            try:
                with self.world.lock:
                    done = self.dispatch(tag, verb, args, literals)
            except Exception as exc:  # a server bug should fail the test loudly, not hang it
                self.send(f"{tag} BAD internal error: {exc!r}")
                continue
            if done:
                return

    def dispatch(self, tag: str, verb: str, args: str, literals: list[bytes]) -> bool:
        if verb == "CAPABILITY":
            self.send(f"* CAPABILITY {self.world.capabilities}")
            self.send(f"{tag} OK CAPABILITY completed")
        elif verb == "NOOP":
            self.send(f"{tag} OK NOOP completed")
        elif verb == "LOGOUT":
            self.send("* BYE logging out")
            self.send(f"{tag} OK LOGOUT completed")
            return True
        elif verb == "LOGIN":
            username, password = _split_args(args)[:2]
            user = self.world.users.get(username)
            if user and user.password == password:
                self.user = user
                self.send(f"{tag} OK LOGIN completed")
            else:
                self.send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials")
        elif verb == "AUTHENTICATE":
            self.send("+ ")
            response = base64.b64decode(self.rfile.readline().strip() or b"")
            match = re.match(rb"user=([^\x01]+)\x01auth=Bearer ([^\x01]+)\x01\x01", response)
            username = self.world.tokens.get(match.group(2).decode()) if match else None
            if match and username == match.group(1).decode() and username in self.world.users:
                self.user = self.world.users[username]
                self.send(f"{tag} OK AUTHENTICATE completed")
            else:
                self.send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid token")
        elif self.user is None:
            self.send(f"{tag} NO Not authenticated")
        elif verb == "LIST":
            for box in self.user.mailboxes.values():
                flags = "\\HasNoChildren" + (f" {box.special}" if box.special else "")
                self.send(f'* LIST ({flags}) "/" "{box.name}"')
            self.send(f"{tag} OK LIST completed")
        elif verb in ("SELECT", "EXAMINE"):
            name = _split_args(args)[0]
            box = self.user.mailboxes.get(name) or self.user.mailboxes.get(name.upper())
            if box is None:
                self.send(f"{tag} NO [NONEXISTENT] No such mailbox")
                return False
            self.box, self.readonly = box, verb == "EXAMINE"
            self.send(f"* {len(box.messages)} EXISTS")
            self.send("* 0 RECENT")
            self.send("* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
            self.send(f"* OK [UIDVALIDITY {box.uidvalidity}] UIDs valid")
            self.send(f"* OK [UIDNEXT {box.uidnext}] Predicted next UID")
            self.send(f"{tag} OK [{'READ-ONLY' if self.readonly else 'READ-WRITE'}] {verb} completed")
        elif verb == "APPEND":
            parts = _split_args(args)
            box = self.user.mailboxes.get(parts[0])
            if box is None:
                self.send(f"{tag} NO [TRYCREATE] No such mailbox")
                return False
            flags = set()
            for part in parts[1:]:
                if part.startswith("("):
                    flags = set(part.strip("()").split())
            stored = box.add(literals[-1], flags)
            self.send(f"{tag} OK [APPENDUID {box.uidvalidity} {stored.uid}] APPEND completed")
        elif self.box is None:
            self.send(f"{tag} BAD No mailbox selected")
        elif verb == "UID SEARCH":
            self.send("* SEARCH " + " ".join(str(m.uid) for m in self.search(args)))
            self.send(f"{tag} OK SEARCH completed")
        elif verb == "UID FETCH":
            spec, _, items = args.partition(" ")
            for message in self.box.by_uids(spec):
                self.fetch(message, items)
            self.send(f"{tag} OK FETCH completed")
        elif verb == "UID STORE":
            spec, operation, value = args.split(" ", 2)
            if self.readonly:
                self.send(f"{tag} NO Mailbox is read-only")
                return False
            flags = set(value.strip("()").split())
            for message in self.box.by_uids(spec):
                if operation.upper().startswith("+"):
                    message.flags |= flags
                elif operation.upper().startswith("-"):
                    message.flags -= flags
                else:
                    message.flags = set(flags)
                seq = self.box.messages.index(message) + 1
                self.send(f"* {seq} FETCH (UID {message.uid} FLAGS ({' '.join(sorted(message.flags))}))")
            self.send(f"{tag} OK STORE completed")
        elif verb in ("UID COPY", "UID MOVE"):
            spec, _, target = args.partition(" ")
            box = self.user.mailboxes.get(_split_args(target)[0])
            if box is None:
                self.send(f"{tag} NO [TRYCREATE] No such mailbox")
                return False
            for message in self.box.by_uids(spec):
                box.add(message.raw, set(message.flags), message.internal)
                if verb == "UID MOVE":
                    seq = self.box.messages.index(message) + 1
                    self.box.messages.remove(message)
                    self.send(f"* {seq} EXPUNGE")
            self.send(f"{tag} OK {verb[4:]} completed")
        elif verb == "EXPUNGE":
            for message in [m for m in self.box.messages if "\\Deleted" in m.flags]:
                seq = self.box.messages.index(message) + 1
                self.box.messages.remove(message)
                self.send(f"* {seq} EXPUNGE")
            self.send(f"{tag} OK EXPUNGE completed")
        elif verb == "CLOSE":
            self.box = None
            self.send(f"{tag} OK CLOSE completed")
        else:
            self.send(f"{tag} BAD Unknown command {verb}")
        return False

    def search(self, criteria: str) -> list[Stored]:
        assert self.box is not None
        found = list(self.box.messages)
        tokens = criteria.split()
        index = 0
        while index < len(tokens):
            key = tokens[index].upper()
            if key == "ALL":
                pass
            elif key == "UID":
                index += 1
                allowed = self.box.by_uids(tokens[index])
                found = [m for m in found if m in allowed]
            elif key == "SINCE":
                index += 1
                day = datetime.strptime(tokens[index].strip('"'), "%d-%b-%Y").date()
                found = [m for m in found if m.internal.date() >= day]
            elif key == "UNSEEN":
                found = [m for m in found if "\\Seen" not in m.flags]
            else:
                raise ValueError(f"unsupported search key {key}")
            index += 1
        return found

    def fetch(self, message: Stored, items: str) -> None:
        assert self.box is not None
        seq = self.box.messages.index(message) + 1
        head, _, text = message.raw.partition(b"\r\n\r\n")
        if not text and b"\n\n" in message.raw:
            head, _, text = message.raw.partition(b"\n\n")
        out: list[bytes] = [f"UID {message.uid}".encode()]
        for item in re.findall(r"BODY(?:\.PEEK)?\[[^\]]*\](?:<[\d.]+>)?|[A-Z0-9.]+", items.strip("()")):
            upper = item.upper()
            if upper == "UID":
                continue
            if upper == "FLAGS":
                out.append(f"FLAGS ({' '.join(sorted(message.flags))})".encode())
            elif upper == "RFC822.SIZE":
                out.append(f"RFC822.SIZE {len(message.raw)}".encode())
            elif upper.startswith("BODY"):
                section = re.search(r"\[([^\]]*)\]", item).group(1).upper()
                partial = re.search(r"<(\d+)\.(\d+)>", item)
                data = message.raw if section == "" else head + b"\r\n\r\n" if section == "HEADER" else text
                label = f"BODY[{section}]"
                if partial:
                    start, length = int(partial.group(1)), int(partial.group(2))
                    data = data[start:start + length]
                    label += f"<{start}>"
                if ".PEEK" not in upper and not self.readonly:
                    message.flags.add("\\Seen")
                out.append(label.encode() + b" {%d}\r\n" % len(data) + data)
            else:
                raise ValueError(f"unsupported fetch item {item}")
        self.send(b"* %d FETCH (" % seq + b" ".join(out) + b")")


def _split_args(text: str) -> list[str]:
    """Atoms, "quoted strings" and (lists) from an IMAP argument string."""
    out: list[str] = []
    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"|(\([^)]*\))|(\S+)', text):
        if match.group(1) is not None:
            out.append(re.sub(r"\\(.)", r"\1", match.group(1)))
        else:
            out.append(match.group(2) or match.group(3))
    return out


# -- SMTP ------------------------------------------------------------------------

class SmtpSession:
    def __init__(self, world: MailWorld, rfile, wfile) -> None:
        self.world, self.rfile, self.wfile = world, rfile, wfile
        self.user: User | None = None

    def send(self, line: str) -> None:
        self.wfile.write(line.encode("utf-8") + CRLF)
        self.wfile.flush()

    def run(self) -> None:
        self.send("220 fake.test ESMTP ready")
        sender = ""
        recipients: list[str] = []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                self.world.greetings.append(line.partition(" ")[2])
                self.send("250-fake.test")
                self.send("250-AUTH PLAIN LOGIN XOAUTH2")
                self.send("250 SIZE 20000000")
            elif verb == "AUTH":
                self.auth(line)
            elif verb == "MAIL":
                if self.user is None:
                    self.send("530 5.7.0 Authentication required")
                elif self.world.smtp_fail_next > 0:
                    self.world.smtp_fail_next -= 1
                    self.send("451 4.3.0 Temporary failure, try again later")
                else:
                    sender = re.search(r"<([^>]*)>", line).group(1)
                    recipients = []
                    self.send("250 2.1.0 OK")
            elif verb == "RCPT":
                address = re.search(r"<([^>]*)>", line).group(1).lower()
                if address.endswith("@bounce.invalid"):
                    self.send("550 5.1.1 No such user")
                else:
                    recipients.append(address)
                    self.send("250 2.1.5 OK")
            elif verb == "DATA":
                if not recipients:
                    self.send("503 5.5.1 RCPT first")
                    continue
                self.send("354 End data with <CR><LF>.<CR><LF>")
                chunks: list[bytes] = []
                while True:
                    data = self.rfile.readline()
                    if data in (b".\r\n", b".\n", b""):
                        break
                    chunks.append(data[1:] if data.startswith(b"..") else data)
                body = b"".join(chunks)
                with self.world.lock:
                    self.world.delivered.append({"from": sender, "to": list(recipients), "raw": body})
                    for address in recipients:
                        target = self.world.user_for(address)
                        if target is not None:
                            target.mailboxes["INBOX"].add(body)
                self.send("250 2.0.0 Queued")
            elif verb == "RSET":
                sender, recipients = "", []
                self.send("250 2.0.0 OK")
            elif verb == "NOOP":
                self.send("250 2.0.0 OK")
            elif verb == "QUIT":
                self.send("221 2.0.0 Bye")
                return
            else:
                self.send("502 5.5.2 Command not recognised")

    def auth(self, line: str) -> None:
        parts = line.split()
        mechanism = parts[1].upper() if len(parts) > 1 else ""
        if mechanism == "PLAIN":
            blob = parts[2] if len(parts) > 2 else self.prompt("")
            _, username, password = base64.b64decode(blob).decode().split("\x00")
        elif mechanism == "LOGIN":
            username = base64.b64decode(parts[2] if len(parts) > 2 else self.prompt("VXNlcm5hbWU6")).decode()
            password = base64.b64decode(self.prompt("UGFzc3dvcmQ6")).decode()
        elif mechanism == "XOAUTH2":
            blob = base64.b64decode(parts[2] if len(parts) > 2 else self.prompt(""))
            match = re.match(rb"user=([^\x01]+)\x01auth=Bearer ([^\x01]+)\x01\x01", blob)
            username = match.group(1).decode() if match else ""
            token_user = self.world.tokens.get(match.group(2).decode()) if match else None
            if match and token_user == username and username in self.world.users:
                self.user = self.world.users[username]
                self.send("235 2.7.0 Authentication successful")
            else:
                self.send("535 5.7.8 Invalid token")
            return
        else:
            self.send("504 5.5.4 Unrecognised authentication type")
            return
        user = self.world.users.get(username)
        if user and user.password == password:
            self.user = user
            self.send("235 2.7.0 Authentication successful")
        else:
            self.send("535 5.7.8 Username and Password not accepted")

    def prompt(self, challenge: str) -> str:
        self.send(f"334 {challenge}")
        return self.rfile.readline().decode().strip()
