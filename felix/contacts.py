"""People Felix knows: names, addresses, numbers and who counts as a VIP.

Contacts are learned from the phone's address book, mail senders and chat
names, and are what lets "message Ravi" resolve to the right Ravi.
"""

from __future__ import annotations

import difflib
import re
from typing import TYPE_CHECKING, Any

from .tools import LOCAL, READ, Registry, ToolContext, ToolError
from .util import fold

if TYPE_CHECKING:  # pragma: no cover
    from .app import Felix


def norm_phone(value: str | None) -> str:
    """Digits only, keeping a leading +. '' when it is not a phone number."""
    if not value:
        return ""
    value = value.strip()
    digits = re.sub(r"\D", "", value)
    if len(digits) < 7 or len(digits) > 15:
        return ""
    return ("+" if value.startswith("+") else "") + digits


def phone_key(value: str | None) -> str:
    """Comparison key: the last 10 digits (ignores country-code formatting)."""
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 7 else ""


def norm_email(value: str | None) -> str:
    value = (value or "").strip().lower()
    return value if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) else ""


class Contacts:
    def __init__(self, app: "Felix") -> None:
        self.app = app

    # -- writing ---------------------------------------------------------------
    def upsert(self, name: str, emails: list[str] | None = None, phones: list[str] | None = None,
               source: str = "manual", org: str | None = None, vip: bool | None = None,
               notes: str | None = None) -> int:
        name = (name or "").strip()
        emails = [e for e in (norm_email(x) for x in emails or []) if e]
        phones = [p for p in (norm_phone(x) for x in phones or []) if p]
        if not name and not emails and not phones:
            return 0
        existing = self._match_existing(name, emails, phones)
        now = self.app.clock.ts()
        if existing:
            merged_emails = _merge(existing["emails"], emails)
            merged_phones = _merge_phones(existing["phones"], phones)
            values: dict[str, Any] = {"emails": merged_emails, "phones": merged_phones, "updated_at": now}
            # A real name beats a bare address used as a placeholder name.
            if name and (not existing["name"] or "@" in existing["name"] or _looks_like_number(existing["name"])):
                values["name"] = name
            if org:
                values["org"] = org
            if vip is not None:
                values["vip"] = int(vip)
            if notes:
                values["notes"] = notes
            self.app.store.update("contacts", values, "id = ?", (existing["id"],))
            return int(existing["id"])
        return self.app.store.insert("contacts", {
            "name": name or (emails[0] if emails else phones[0]), "emails": emails, "phones": phones,
            "org": org, "vip": int(bool(vip)), "notes": notes, "source": source, "updated_at": now,
        })

    def _match_existing(self, name: str, emails: list[str], phones: list[str]) -> dict[str, Any] | None:
        rows = self.all()
        keys = {phone_key(p) for p in phones}
        for row in rows:
            if set(row["emails"]) & set(emails):
                return row
            if keys & {phone_key(p) for p in row["phones"]}:
                return row
        if name and not emails and not phones:
            folded = fold(name)
            for row in rows:
                if fold(row["name"]) == folded:
                    return row
        return None

    # -- reading ---------------------------------------------------------------
    def all(self) -> list[dict[str, Any]]:
        return self.app.store.query("SELECT * FROM contacts ORDER BY name COLLATE NOCASE")

    def get(self, contact_id: int) -> dict[str, Any] | None:
        return self.app.store.one("SELECT * FROM contacts WHERE id = ?", (contact_id,))

    def by_address(self, address: str | None) -> dict[str, Any] | None:
        email, key = norm_email(address), phone_key(address)
        if not email and not key:
            return None
        for row in self.all():
            if email and email in row["emails"]:
                return row
            if key and key in {phone_key(p) for p in row["phones"]}:
                return row
        return None

    def name_for(self, address: str | None, fallback: str | None = None) -> str:
        row = self.by_address(address)
        return row["name"] if row else (fallback or address or "")

    def is_vip(self, *identities: str | None) -> bool:
        """True when any of the given names / addresses belongs to a VIP."""
        vips = [fold(v) for v in self.app.config.get("owner.vips", []) or [] if v]
        vip_keys = {phone_key(v) for v in vips} - {""}
        for identity in identities:
            if not identity:
                continue
            folded = fold(identity)
            if folded in vips or (phone_key(identity) and phone_key(identity) in vip_keys):
                return True
            if any(v and (v in folded or folded.endswith("@" + v.lstrip("@"))) for v in vips if len(v) > 3):
                return True
            row = self.by_address(identity)
            if row is None:
                matches = [r for r in self.all() if fold(r["name"]) == folded]
                row = matches[0] if len(matches) == 1 else None
            if row and row["vip"]:
                return True
        return False

    def resolve(self, query: str, want: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Rank contacts matching ``query``. ``want`` is 'email' or 'phone' to
        keep only people reachable that way."""
        query = (query or "").strip()
        if not query:
            return []
        email, phone = norm_email(query), norm_phone(query)
        if email or (phone and not re.search(r"[A-Za-z]", query)):
            row = self.by_address(email or phone)
            if row:
                return [dict(row, score=1.0)]
            literal = {"id": 0, "name": email or phone, "emails": [email] if email else [],
                       "phones": [phone] if phone else [], "vip": 0, "score": 1.0}
            return [literal]
        folded = fold(query)
        tokens = folded.split()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in self.all():
            if want == "email" and not row["emails"]:
                continue
            if want == "phone" and not row["phones"]:
                continue
            name = fold(row["name"])
            parts = name.split()
            if name == folded:
                score = 1.0
            elif all(any(p.startswith(t) for p in parts) for t in tokens):
                score = 0.9 if parts and parts[0].startswith(tokens[0]) else 0.82
            elif folded in name:
                score = 0.75
            else:
                ratio = max(
                    [difflib.SequenceMatcher(None, folded, name).ratio()]
                    + [difflib.SequenceMatcher(None, folded, p).ratio() for p in parts]
                )
                score = ratio * 0.8 if ratio >= 0.78 else 0.0
            if score:
                scored.append((score + (0.03 if row["vip"] else 0), row))
        scored.sort(key=lambda item: (-item[0], item[1]["name"].lower()))
        return [dict(row, score=round(min(score, 1.0), 2)) for score, row in scored[:limit]]

    def pick(self, query: str, want: str) -> dict[str, Any]:
        """Resolve to exactly one person or raise a ToolError that says why."""
        matches = self.resolve(query, want=want)
        if not matches:
            raise ToolError(
                f"I don't know a contact called {query!r} with a{'n email address' if want == 'email' else ' phone number'}. "
                "Give me the address or number directly, or save the contact first."
            )
        best = matches[0]
        close = [m for m in matches[1:] if best["score"] - m["score"] < 0.08]
        if close and best["score"] < 1.0:
            names = ", ".join(m["name"] for m in [best] + close)
            raise ToolError(f"{query!r} matches more than one person: {names}. Which one?")
        return best

    def address_of(self, query: str, want: str) -> tuple[str, str]:
        """Return (address, display name) for an email or phone lookup."""
        person = self.pick(query, want)
        values = person["emails"] if want == "email" else person["phones"]
        if not values:
            raise ToolError(f"{person['name']} has no {'email address' if want == 'email' else 'phone number'} on file.")
        return values[0], person["name"]

    # -- tools -----------------------------------------------------------------
    def register(self, tools: Registry) -> None:
        @tools.tool(
            "contact_find",
            "Look up a person in the owner's contacts by name, email or phone number. "
            "Use it to get an address before sending a message when only a name is known.",
            {"query": {"type": "string", "description": "Name, part of a name, email or number"}},
            ["query"], risk=READ, group="contacts",
        )
        def contact_find(args: dict[str, Any], ctx: ToolContext) -> Any:
            found = self.resolve(args["query"], limit=8)
            return {"contacts": [_public(c) for c in found]}

        @tools.tool(
            "contact_save",
            "Create or update a contact. Set vip to true for people whose messages should always rank first.",
            {
                "name": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string", "description": "Preferably with country code, e.g. +919812345678"},
                "org": {"type": "string"},
                "vip": {"type": "boolean"},
                "notes": {"type": "string"},
            },
            ["name"], risk=LOCAL, group="contacts",
        )
        def contact_save(args: dict[str, Any], ctx: ToolContext) -> Any:
            if args.get("email") and not norm_email(args["email"]):
                raise ToolError(f"{args['email']!r} is not a valid email address.")
            if args.get("phone") and not norm_phone(args["phone"]):
                raise ToolError(f"{args['phone']!r} is not a valid phone number.")
            contact_id = self.upsert(
                args["name"], [args["email"]] if args.get("email") else [],
                [args["phone"]] if args.get("phone") else [], source="manual",
                org=args.get("org"), vip=args.get("vip"), notes=args.get("notes"),
            )
            saved = self.get(contact_id)
            return {"message": f"Saved {saved['name']}.", "contact": _public(saved)}


def _public(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in ("id", "name", "emails", "phones", "org", "vip", "notes")}
    out["vip"] = bool(out.get("vip"))
    if "score" in row:
        out["match"] = row["score"]
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def _merge(existing: list[str], extra: list[str]) -> list[str]:
    out = list(existing)
    for item in extra:
        if item not in out:
            out.append(item)
    return out


def _merge_phones(existing: list[str], extra: list[str]) -> list[str]:
    out = list(existing)
    keys = {phone_key(p) for p in out}
    for item in extra:
        if phone_key(item) not in keys:
            out.append(item)
            keys.add(phone_key(item))
    return out


def _looks_like_number(text: str) -> bool:
    return bool(re.fullmatch(r"[+\d\s()-]{7,}", text or ""))
