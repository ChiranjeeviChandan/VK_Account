// Felix <-> WhatsApp linked-device bridge: the part that does not depend on the
// WhatsApp library. It turns library events into Felix's JSON-lines protocol and
// Felix's commands into library calls, so it can be tested with a scripted
// connection (see test/bridge.test.js). index.js supplies the real connection.
//
// Felix -> bridge (one JSON object per line on stdin)
//   {"id": 7, "cmd": "send", "to": "<jid>", "text": "..."}
//   {"id": 8, "cmd": "pairing_code", "phone": "+919800000001"}
//   {"id": 9, "cmd": "logout"}            {"cmd": "shutdown"}
// bridge -> Felix (one JSON object per line on stdout)
//   {"event": "state", "state": "qr|pairing|open|reconnecting|closed|logged_out|error", "qr", "pairing_code", "me", "error"}
//   {"event": "message", "id", "chat", "chat_name", "is_group", "sender_name", "from_me", "text", "ts"}
//   {"event": "contacts", "contacts": [{"jid", "name"}]}
//   {"reply": 7, "ok": true, "id": "<message id>"}     {"reply": 7, "ok": false, "error": "..."}

import { createInterface } from "node:readline";

export const LOGGED_OUT = 401;
export const REPLACED = 440;
const USER = "@s.whatsapp.net";
const LID = "@lid";
const GROUP = "@g.us";
const RECONNECT_DELAYS = [1000, 2000, 5000, 15000, 30000, 60000];
const WRAPPERS = ["ephemeralMessage", "viewOnceMessage", "viewOnceMessageV2", "viewOnceMessageV2Extension",
  "documentWithCaptionMessage", "editedMessage"];

/** "919800000001:12@s.whatsapp.net" -> "919800000001@s.whatsapp.net" (drops the device part). */
export function bare(jid) {
  if (!jid || !jid.includes("@")) return jid || "";
  const [user, server] = jid.split("@");
  return `${user.split(":")[0]}@${server}`;
}

const numberOf = (jid) => (jid && jid.endsWith(USER) ? `+${jid.split("@")[0].split(":")[0]}` : "");

/** The text of a WhatsApp message, or a short placeholder for media. "" means: nothing to tell Felix. */
export function textOf(content) {
  let message = content;
  for (let depth = 0; message && depth < 4; depth += 1) {
    const wrapper = WRAPPERS.find((name) => message[name]?.message);
    if (!wrapper) break;
    message = message[wrapper].message;
  }
  if (!message) return "";
  if (typeof message.conversation === "string" && message.conversation) return message.conversation;
  if (message.extendedTextMessage?.text) return message.extendedTextMessage.text;
  const caption = (kind, label) => (message[kind] ? [`[${label}]`, message[kind].caption].filter(Boolean).join(" ") : null);
  return caption("imageMessage", "photo")
    ?? caption("videoMessage", "video")
    ?? (message.documentMessage
      ? [`[document${message.documentMessage.fileName ? `: ${message.documentMessage.fileName}` : ""}]`,
        message.documentMessage.caption].filter(Boolean).join(" ") : null)
    ?? (message.audioMessage ? (message.audioMessage.ptt ? "[voice message]" : "[audio]") : null)
    ?? (message.stickerMessage ? "[sticker]" : null)
    ?? (message.locationMessage || message.liveLocationMessage ? "[location]" : null)
    ?? (message.contactMessage ? `[contact: ${message.contactMessage.displayName || "card"}]` : null)
    ?? (message.contactsArrayMessage ? "[contacts]" : null)
    ?? (message.pollCreationMessage || message.pollCreationMessageV3
      ? `[poll] ${(message.pollCreationMessage || message.pollCreationMessageV3).name || ""}`.trim() : null)
    ?? "";       // reactions, receipts, edits, deletions and key exchanges are not messages for the owner
}

/**
 * @param {object} options
 * @param {() => Promise<object>} options.connect   opens a connection; resolves to the library's socket
 * @param {(line: object) => void} options.emit      writes one protocol object to Felix
 * @param {() => Promise<void>} [options.forget]     deletes the saved session (after a log-out)
 * @param {(code: number) => void} [options.exit]    ends the process
 * @param {(fn: Function, ms: number) => any} [options.later]
 */
export function createBridge({ connect, emit, forget = async () => {}, exit = () => {}, later = setTimeout }) {
  let socket = null;
  let open = false;
  let stopping = false;
  let attempts = 0;
  let pairingCode = null;
  const names = new Map();        // bare jid -> saved name
  const pnOfLid = new Map();      // "<user>@lid" -> "<number>@s.whatsapp.net"
  const groups = new Map();       // group jid -> subject
  const known = new Set();        // numbers we have seen on WhatsApp
  let contactsDirty = new Map();
  let contactsTimer = null;
  let inbox = Promise.resolve();  // incoming messages are handled strictly in order

  const state = (name, extra = {}) => emit({ event: "state", state: name, ...extra });

  function me() {
    const user = socket?.user;
    if (!user?.id) return undefined;
    const jid = bare(user.phoneNumber || user.id);
    if (user.lid) pnOfLid.set(bare(user.lid), jid);
    return { jid, name: user.name || user.notify || "" };
  }

  /** Prefer a phone-number id over WhatsApp's anonymous "lid" id, so Felix can match its contacts. */
  async function phoneJid(jid, alternative) {
    const id = bare(jid);
    if (!id.endsWith(LID)) return id;
    if (alternative && bare(alternative).endsWith(USER)) {
      pnOfLid.set(id, bare(alternative));
      return bare(alternative);
    }
    if (pnOfLid.has(id)) return pnOfLid.get(id);
    try {
      const found = await socket?.signalRepository?.lidMapping?.getPNForLID?.(id);
      if (found) {
        pnOfLid.set(id, bare(found));
        return bare(found);
      }
    } catch { /* no mapping yet: keep the lid, replies to it still work */ }
    return id;
  }

  function remember(contacts) {
    for (const contact of contacts || []) {
      const lid = contact.lid || (contact.id?.endsWith(LID) ? contact.id : null);
      const phone = contact.phoneNumber || (contact.id?.endsWith(USER) ? contact.id : null);
      if (lid && phone) pnOfLid.set(bare(lid), bare(phone));
      const name = contact.name || contact.verifiedName || contact.notify;
      if (!phone || !name) continue;
      const jid = bare(phone);
      known.add(jid);
      // A name the owner saved beats the name people chose for themselves.
      if (contact.name || !names.has(jid)) {
        if (names.get(jid) !== name) contactsDirty.set(jid, name);
        names.set(jid, name);
      }
    }
    if (contactsDirty.size && !contactsTimer) {
      contactsTimer = later(() => {
        contactsTimer = null;
        const batch = [...contactsDirty].map(([jid, name]) => ({ jid, name }));
        contactsDirty = new Map();
        for (let start = 0; start < batch.length; start += 500) {
          emit({ event: "contacts", contacts: batch.slice(start, start + 500) });
        }
      }, 1500);
    }
  }

  async function groupName(jid) {
    if (groups.has(jid)) return groups.get(jid);
    try {
      const subject = (await socket.groupMetadata(jid))?.subject || "";
      if (subject) groups.set(jid, subject);
      return subject;
    } catch {
      return "";
    }
  }

  async function onMessage(message) {
    const key = message?.key;
    if (!key?.remoteJid || !key.id) return;
    const remote = key.remoteJid;
    if (remote === "status@broadcast" || remote.endsWith("@broadcast") || remote.endsWith("@newsletter")) return;
    const text = textOf(message.message);
    if (!text) return;
    const isGroup = remote.endsWith(GROUP);
    const chat = isGroup ? remote : await phoneJid(remote, key.remoteJidAlt);
    const fromMe = Boolean(key.fromMe);
    let chatName;
    let senderName;
    if (isGroup) {
      chatName = (await groupName(remote)) || "Group";
      const sender = await phoneJid(key.participant || "", key.participantAlt);
      senderName = fromMe ? "" : names.get(sender) || message.pushName || numberOf(sender) || "Someone";
    } else {
      known.add(chat);
      chatName = names.get(chat) || (fromMe ? "" : message.pushName) || numberOf(chat) || chat;
      senderName = fromMe ? "" : chatName;
    }
    const stamp = Number(message.messageTimestamp?.toNumber ? message.messageTimestamp.toNumber() : message.messageTimestamp);
    emit({ event: "message", id: key.id, chat, chat_name: chatName, is_group: isGroup, sender_name: senderName,
      from_me: fromMe, text, ts: Number.isFinite(stamp) && stamp > 0 ? stamp : Math.floor(Date.now() / 1000) });
  }

  async function onConnection(update) {
    if (update.qr) {
      pairingCode = null;
      state("qr", { qr: update.qr });
    }
    if (update.connection === "open") {
      open = true;
      attempts = 0;
      pairingCode = null;
      state("open", { me: me() });
    } else if (update.connection === "close") {
      open = false;
      if (stopping) return;
      const error = update.lastDisconnect?.error;
      const code = error?.output?.statusCode;
      if (code === LOGGED_OUT) {
        // The owner removed this linked device (or asked Felix to log out): forget the session
        // so the next start offers a fresh code instead of failing forever.
        await forget().catch(() => {});
        state("logged_out");
        exit(0);
        return;
      }
      if (code === REPLACED) {
        state("closed", { error: "WhatsApp opened this session somewhere else" });
        exit(0);
        return;
      }
      const delay = RECONNECT_DELAYS[Math.min(attempts, RECONNECT_DELAYS.length - 1)];
      attempts += 1;
      // 515 right after linking is WhatsApp asking for a fresh connection: reconnect at once.
      const wait = code === 515 ? 0 : delay;
      if (wait) state("reconnecting", { error: error?.message || "connection lost" });
      later(() => start().catch(fail), wait);
    }
  }

  function fail(error) {
    state("error", { error: String(error?.message || error) });
    exit(1);
  }

  async function start() {
    if (stopping) return;
    socket = await connect();
    socket.ev.on("connection.update", (update) => onConnection(update).catch(fail));
    socket.ev.on("messages.upsert", ({ messages }) => {
      // Both live ("notify") messages and ones that arrived while the bridge was away ("append").
      // One at a time: name lookups are asynchronous, and Felix must see messages in the order they came.
      for (const message of messages || []) inbox = inbox.then(() => onMessage(message)).catch(() => {});
    });
    socket.ev.on("contacts.upsert", remember);
    socket.ev.on("contacts.update", remember);
    socket.ev.on("messaging-history.set", ({ contacts }) => remember(contacts));
    socket.ev.on("lid-mapping.update", (pair) => {
      if (pair?.lid && pair?.pn) pnOfLid.set(bare(pair.lid), bare(pair.pn));
    });
    socket.ev.on("groups.update", (updates) => {
      for (const update of updates || []) if (update.id && update.subject) groups.set(update.id, update.subject);
    });
  }

  const commands = {
    async send({ to, text }) {
      if (!open) throw new Error("WhatsApp is not connected");
      if (typeof to !== "string" || !to.includes("@") || typeof text !== "string" || !text.trim()) {
        throw new Error("send needs a WhatsApp id and some text");
      }
      const jid = bare(to);
      if (jid.endsWith(USER) && !known.has(jid)) {
        // A number Felix has never seen: make sure it is on WhatsApp, or the message vanishes silently.
        const [found] = (await socket.onWhatsApp(jid.split("@")[0])) || [];
        if (!found?.exists) throw new Error(`${numberOf(jid)} is not on WhatsApp`);
        known.add(jid);
      }
      const sent = await socket.sendMessage(jid, { text });
      if (!sent?.key?.id) throw new Error("WhatsApp did not accept the message");
      return { id: sent.key.id };
    },
    async pairing_code({ phone }) {
      const digits = String(phone || "").replace(/\D/g, "");
      if (digits.length < 8) throw new Error("give the phone number with its country code");
      if (open) throw new Error("WhatsApp is already linked");
      pairingCode = await socket.requestPairingCode(digits);
      state("pairing", { pairing_code: pairingCode });
      return { code: pairingCode };
    },
    async logout() {
      if (!socket) throw new Error("WhatsApp is not connected");
      await socket.logout();
      return {};
    },
  };

  /** Handle one command object from Felix. */
  async function handle(command) {
    if (!command || typeof command !== "object") return;
    if (command.cmd === "shutdown") {
      await stop();
      exit(0);
      return;
    }
    const run = commands[command.cmd];
    if (command.id === undefined) return;
    if (!run) {
      emit({ reply: command.id, ok: false, error: `unknown command: ${command.cmd}` });
      return;
    }
    try {
      emit({ reply: command.id, ok: true, ...(await run(command)) });
    } catch (error) {
      emit({ reply: command.id, ok: false, error: String(error?.message || error) });
    }
  }

  async function stop() {
    stopping = true;
    open = false;
    try { await socket?.end?.(undefined); } catch { /* already closed */ }
  }

  return { start, handle, stop, fail };
}

/** Wire a bridge to this process's stdin and stdout. Nothing but protocol lines may reach stdout. */
export function runStdio({ connect, forget, input = process.stdin, output = process.stdout, exit = (code) => process.exit(code) }) {
  const emit = (line) => output.write(`${JSON.stringify(line)}\n`);
  const bridge = createBridge({ connect, emit, forget, exit });
  const lines = createInterface({ input, crlfDelay: Infinity });
  lines.on("line", (line) => {
    let command;
    try { command = JSON.parse(line); } catch { return; }
    bridge.handle(command).catch(() => {});
  });
  // Felix closed our stdin (it stopped or crashed): do not linger as an orphan.
  lines.on("close", () => bridge.stop().finally(() => exit(0)));
  emit({ event: "state", state: "starting" });
  bridge.start().catch(bridge.fail);
  return bridge;
}
