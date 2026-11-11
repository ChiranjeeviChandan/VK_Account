// Tests for the bridge logic with a scripted WhatsApp connection: no network, no account.
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { test } from "node:test";

import { bare, createBridge, runStdio, textOf } from "../bridge.js";

const ME = { id: "919800000001:7@s.whatsapp.net", lid: "55500011122233:7@lid", name: "VK" };

/** A stand-in for the library's socket: tests fire its events and inspect what was sent. */
function fakeSocket(overrides = {}) {
  const socket = {
    ev: new EventEmitter(),
    user: undefined,
    sent: [],
    ended: false,
    async sendMessage(jid, content) {
      socket.sent.push([jid, content.text]);
      return { key: { id: `MSG${socket.sent.length}`, remoteJid: jid, fromMe: true } };
    },
    async onWhatsApp(number) { return [{ jid: `${number}@s.whatsapp.net`, exists: !number.startsWith("000") }]; },
    async groupMetadata(jid) { return { id: jid, subject: "Leadership" }; },
    async requestPairingCode(digits) { return `CODE${digits.slice(-4)}`; },
    async logout() { socket.loggedOut = true; },
    async end() { socket.ended = true; },
    signalRepository: { lidMapping: { async getPNForLID(lid) { return lid === "777@lid" ? "919877766655@s.whatsapp.net" : null; } } },
    ...overrides,
  };
  return socket;
}

/** A bridge wired to fake sockets, with timers that tests fire by hand. */
function harness(options = {}) {
  const lines = [];
  const sockets = [];
  const timers = [];
  const exits = [];
  let forgotten = 0;
  const bridge = createBridge({
    connect: async () => { sockets.push(fakeSocket(options.socket)); return sockets.at(-1); },
    emit: (line) => lines.push(line),
    forget: async () => { forgotten += 1; },
    exit: (code) => exits.push(code),
    later: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
  });
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  return {
    bridge, lines, sockets, timers, exits, settle,
    get socket() { return sockets.at(-1); },
    get forgotten() { return forgotten; },
    async open() {
      await bridge.start();
      sockets.at(-1).user = ME;
      sockets.at(-1).ev.emit("connection.update", { connection: "open" });
      await settle();
    },
    async runTimers() { for (const timer of timers.splice(0)) await timer.fn(); await settle(); },
    events(name) { return lines.filter((line) => line.event === name); },
    replies() { return lines.filter((line) => "reply" in line); },
  };
}

const incoming = (fields = {}, content = { conversation: "Are we on for 4pm?" }) => ({
  key: { remoteJid: "919812345678@s.whatsapp.net", id: "IN1", fromMe: false, ...fields.key },
  message: content, pushName: "Ravi K", messageTimestamp: 1794400000, ...fields.top,
});

test("text and media are reduced to what Felix needs to read", () => {
  assert.equal(textOf({ conversation: "hello" }), "hello");
  assert.equal(textOf({ extendedTextMessage: { text: "see https://example.com" } }), "see https://example.com");
  assert.equal(textOf({ imageMessage: { caption: "the receipt" } }), "[photo] the receipt");
  assert.equal(textOf({ imageMessage: {} }), "[photo]");
  assert.equal(textOf({ audioMessage: { ptt: true } }), "[voice message]");
  assert.equal(textOf({ documentMessage: { fileName: "Q4.pdf", caption: "final" } }), "[document: Q4.pdf] final");
  assert.equal(textOf({ ephemeralMessage: { message: { conversation: "disappearing" } } }), "disappearing");
  assert.equal(textOf({ viewOnceMessageV2: { message: { videoMessage: {} } } }), "[video]");
  assert.equal(textOf({ documentWithCaptionMessage: { message: { documentMessage: { fileName: "a.pdf" } } } }), "[document: a.pdf]");
  // not messages for the owner: reactions, deletions, key exchanges, nothing at all
  assert.equal(textOf({ reactionMessage: { text: "👍" } }), "");
  assert.equal(textOf({ protocolMessage: { type: 0 } }), "");
  assert.equal(textOf(undefined), "");
  assert.equal(bare("919800000001:12@s.whatsapp.net"), "919800000001@s.whatsapp.net");
});

test("linking: QR, then open with the owner's own id", async () => {
  const h = harness();
  await h.bridge.start();
  h.socket.ev.emit("connection.update", { qr: "2@qr-payload" });
  await h.settle();
  assert.deepEqual(h.events("state").at(-1), { event: "state", state: "qr", qr: "2@qr-payload" });
  h.socket.user = ME;
  h.socket.ev.emit("connection.update", { connection: "open" });
  await h.settle();
  assert.deepEqual(h.events("state").at(-1),
    { event: "state", state: "open", me: { jid: "919800000001@s.whatsapp.net", name: "VK" } });
});

test("linking with a code instead of the camera", async () => {
  const h = harness();
  await h.bridge.start();
  await h.bridge.handle({ id: 1, cmd: "pairing_code", phone: "+91 98000 00001" });
  assert.deepEqual(h.replies(), [{ reply: 1, ok: true, code: "CODE0001" }]);
  assert.deepEqual(h.events("state").at(-1), { event: "state", state: "pairing", pairing_code: "CODE0001" });
  await h.bridge.handle({ id: 2, cmd: "pairing_code", phone: "12" });
  assert.equal(h.replies().at(-1).ok, false);
});

test("an incoming chat message reaches Felix with names and time", async () => {
  const h = harness();
  await h.open();
  h.socket.ev.emit("contacts.upsert", [{ id: "919812345678@s.whatsapp.net", name: "Ravi Kumar", notify: "Ravi K" }]);
  h.socket.ev.emit("messages.upsert", { type: "notify", messages: [incoming()] });
  await h.settle();
  assert.deepEqual(h.events("message"), [{
    event: "message", id: "IN1", chat: "919812345678@s.whatsapp.net", chat_name: "Ravi Kumar", is_group: false,
    sender_name: "Ravi Kumar", from_me: false, text: "Are we on for 4pm?", ts: 1794400000 }]);
  // an unknown sender is shown by the name they chose, then by number
  h.socket.ev.emit("messages.upsert", { type: "append", messages: [
    incoming({ key: { remoteJid: "14155550123@s.whatsapp.net", id: "IN2" }, top: { pushName: "Daniel" } }),
    incoming({ key: { remoteJid: "447700900123@s.whatsapp.net", id: "IN3" }, top: { pushName: undefined } })] });
  await h.settle();
  assert.deepEqual(h.events("message").slice(1).map((m) => [m.chat_name, m.sender_name]),
    [["Daniel", "Daniel"], ["+447700900123", "+447700900123"]]);
});

test("group messages carry the group name and who spoke", async () => {
  const h = harness();
  await h.open();
  h.socket.ev.emit("contacts.upsert", [{ id: "919800011122@s.whatsapp.net", name: "Meera Iyer" }]);
  h.socket.ev.emit("messages.upsert", { type: "notify", messages: [incoming({
    key: { remoteJid: "120363041234567890@g.us", id: "G1", participant: "919800011122:3@s.whatsapp.net" } },
  { conversation: "Board deck draft 3 is out" })] });
  await h.settle();
  const message = h.events("message")[0];
  assert.deepEqual([message.chat, message.chat_name, message.is_group, message.sender_name],
    ["120363041234567890@g.us", "Leadership", true, "Meera Iyer"]);
});

test("WhatsApp's anonymous ids are turned back into phone numbers where possible", async () => {
  const h = harness();
  await h.open();
  // 1. the message itself carries the phone-number id
  h.socket.ev.emit("messages.upsert", { type: "notify", messages: [incoming({
    key: { remoteJid: "111@lid", remoteJidAlt: "919812345678@s.whatsapp.net", id: "L1" } })] });
  // 2. a mapping announced earlier
  h.socket.ev.emit("lid-mapping.update", { lid: "222@lid", pn: "919800011122@s.whatsapp.net" });
  h.socket.ev.emit("messages.upsert", { type: "notify", messages: [incoming({ key: { remoteJid: "222@lid", id: "L2" } })] });
  // 3. the library's own store
  h.socket.ev.emit("messages.upsert", { type: "notify", messages: [incoming({ key: { remoteJid: "777@lid", id: "L3" } })] });
  // 4. nothing known: the id is kept, so a reply still reaches the chat
  h.socket.ev.emit("messages.upsert", { type: "notify", messages: [incoming({ key: { remoteJid: "999@lid", id: "L4" } })] });
  // 5. the owner's own "Message yourself" chat arrives under their anonymous id
  h.socket.ev.emit("messages.upsert", { type: "notify", messages: [incoming({
    key: { remoteJid: "55500011122233@lid", id: "L5", fromMe: true } }, { conversation: "what's broken" })] });
  await h.settle();
  assert.deepEqual(h.events("message").map((m) => m.chat), ["919812345678@s.whatsapp.net", "919800011122@s.whatsapp.net",
    "919877766655@s.whatsapp.net", "999@lid", "919800000001@s.whatsapp.net"]);
  assert.equal(h.events("message").at(-1).from_me, true);
});

test("status updates, channels and empty events are ignored", async () => {
  const h = harness();
  await h.open();
  h.socket.ev.emit("messages.upsert", { type: "notify", messages: [
    incoming({ key: { remoteJid: "status@broadcast", id: "S1" } }),
    incoming({ key: { remoteJid: "12345@newsletter", id: "S2" } }),
    incoming({ key: { id: "S3" } }, { reactionMessage: { text: "👍" } }),
    incoming({ key: { id: "S4" } }, null),
    { key: { remoteJid: "919812345678@s.whatsapp.net" } }] });
  await h.settle();
  assert.deepEqual(h.events("message"), []);
});

test("sending replies with the message id and refuses numbers that are not on WhatsApp", async () => {
  const h = harness();
  await h.bridge.start();
  await h.bridge.handle({ id: 1, cmd: "send", to: "919812345678@s.whatsapp.net", text: "Yes, 4pm works" });
  assert.deepEqual(h.replies().at(-1), { reply: 1, ok: false, error: "WhatsApp is not connected" });
  h.socket.user = ME;
  h.socket.ev.emit("connection.update", { connection: "open" });
  await h.settle();
  await h.bridge.handle({ id: 2, cmd: "send", to: "919812345678@s.whatsapp.net", text: "Yes, 4pm works" });
  assert.deepEqual(h.replies().at(-1), { reply: 2, ok: true, id: "MSG1" });
  assert.deepEqual(h.socket.sent, [["919812345678@s.whatsapp.net", "Yes, 4pm works"]]);
  await h.bridge.handle({ id: 3, cmd: "send", to: "0001112223@s.whatsapp.net", text: "hello" });
  assert.deepEqual(h.replies().at(-1), { reply: 3, ok: false, error: "+0001112223 is not on WhatsApp" });
  await h.bridge.handle({ id: 4, cmd: "send", to: "120363041234567890@g.us", text: "On it" });
  assert.equal(h.replies().at(-1).ok, true);
  await h.bridge.handle({ id: 5, cmd: "send", to: "nobody", text: "" });
  assert.equal(h.replies().at(-1).ok, false);
  await h.bridge.handle({ id: 6, cmd: "dance" });
  assert.deepEqual(h.replies().at(-1), { reply: 6, ok: false, error: "unknown command: dance" });
  assert.equal(h.socket.sent.length, 2);
});

test("a failed send is reported, not thrown", async () => {
  const h = harness({ socket: { async sendMessage() { throw new Error("rate limited"); } } });
  await h.open();
  h.socket.ev.emit("contacts.upsert", [{ id: "919812345678@s.whatsapp.net", name: "Ravi Kumar" }]);
  await h.bridge.handle({ id: 1, cmd: "send", to: "919812345678@s.whatsapp.net", text: "hi" });
  assert.deepEqual(h.replies(), [{ reply: 1, ok: false, error: "rate limited" }]);
});

test("contacts are passed on in batches, saved names first", async () => {
  const h = harness();
  await h.open();
  h.socket.ev.emit("contacts.upsert", [
    { id: "919812345678@s.whatsapp.net", notify: "Ravi K" },
    { id: "111@lid", phoneNumber: "919800011122@s.whatsapp.net", name: "Meera Iyer" },
    { id: "333@lid", name: "No Number" },
    { id: "919800055566@s.whatsapp.net" }]);
  h.socket.ev.emit("contacts.update", [{ id: "919812345678@s.whatsapp.net", name: "Ravi Kumar" }]);
  h.socket.ev.emit("contacts.update", [{ id: "919812345678@s.whatsapp.net", notify: "Ravi again" }]);
  assert.equal(h.timers.length, 1);
  await h.runTimers();
  assert.deepEqual(h.events("contacts"), [{ event: "contacts", contacts: [
    { jid: "919812345678@s.whatsapp.net", name: "Ravi Kumar" }, { jid: "919800011122@s.whatsapp.net", name: "Meera Iyer" }] }]);
  h.socket.ev.emit("messaging-history.set", { contacts: [{ id: "14155550123@s.whatsapp.net", name: "Daniel Park" }] });
  await h.runTimers();
  assert.deepEqual(h.events("contacts").at(-1).contacts, [{ jid: "14155550123@s.whatsapp.net", name: "Daniel Park" }]);
});

test("a dropped connection reconnects by itself with growing pauses", async () => {
  const h = harness();
  await h.open();
  h.socket.ev.emit("connection.update", { connection: "close", lastDisconnect: { error: Object.assign(new Error("Connection lost"), { output: { statusCode: 408 } }) } });
  await h.settle();
  assert.deepEqual(h.events("state").at(-1), { event: "state", state: "reconnecting", error: "Connection lost" });
  assert.equal(h.timers[0].ms, 1000);
  await h.bridge.handle({ id: 1, cmd: "send", to: "919812345678@s.whatsapp.net", text: "hi" });
  assert.equal(h.replies().at(-1).error, "WhatsApp is not connected");
  await h.runTimers();
  assert.equal(h.sockets.length, 2);
  h.socket.ev.emit("connection.update", { connection: "close", lastDisconnect: { error: { output: { statusCode: 408 } } } });
  await h.settle();
  assert.equal(h.timers[0].ms, 2000);
  await h.runTimers();
  h.socket.user = ME;
  h.socket.ev.emit("connection.update", { connection: "open" });
  await h.settle();
  assert.equal(h.events("state").at(-1).state, "open");
  // after a good connection the pauses start small again
  h.socket.ev.emit("connection.update", { connection: "close", lastDisconnect: { error: { output: { statusCode: 428 } } } });
  await h.settle();
  assert.equal(h.timers[0].ms, 1000);
  assert.deepEqual(h.exits, []);
});

test("the restart WhatsApp asks for right after linking happens at once", async () => {
  const h = harness();
  await h.bridge.start();
  h.socket.ev.emit("connection.update", { connection: "close", lastDisconnect: { error: { output: { statusCode: 515 } } } });
  await h.settle();
  assert.equal(h.timers[0].ms, 0);
  assert.equal(h.events("state").some((s) => s.state === "reconnecting"), false);
  await h.runTimers();
  assert.equal(h.sockets.length, 2);
});

test("being logged out forgets the session and stops for good", async () => {
  const h = harness();
  await h.open();
  await h.bridge.handle({ id: 1, cmd: "logout" });
  assert.deepEqual(h.replies(), [{ reply: 1, ok: true }]);
  assert.equal(h.socket.loggedOut, true);
  h.socket.ev.emit("connection.update", { connection: "close", lastDisconnect: { error: { output: { statusCode: 401 } } } });
  await h.settle();
  assert.equal(h.forgotten, 1);
  assert.deepEqual(h.events("state").at(-1), { event: "state", state: "logged_out" });
  assert.deepEqual(h.exits, [0]);
  assert.equal(h.timers.length, 0, "no reconnect after a log-out");
});

test("a session taken over elsewhere is not fought for", async () => {
  const h = harness();
  await h.open();
  h.socket.ev.emit("connection.update", { connection: "close", lastDisconnect: { error: { output: { statusCode: 440 } } } });
  await h.settle();
  assert.equal(h.events("state").at(-1).state, "closed");
  assert.deepEqual([h.exits, h.timers.length, h.forgotten], [[0], 0, 0]);
});

test("shutdown closes the connection quietly", async () => {
  const h = harness();
  await h.open();
  await h.bridge.handle({ cmd: "shutdown" });
  assert.equal(h.socket.ended, true);
  assert.deepEqual(h.exits, [0]);
  h.socket.ev.emit("connection.update", { connection: "close", lastDisconnect: { error: { output: { statusCode: 428 } } } });
  await h.settle();
  assert.equal(h.timers.length, 0, "no reconnect while stopping");
});

test("over stdin and stdout only protocol lines are spoken", async () => {
  const input = new PassThrough();
  const output = new PassThrough();
  const written = [];
  output.on("data", (chunk) => written.push(chunk.toString()));
  const exits = [];
  const socket = fakeSocket();
  runStdio({ connect: async () => socket, input, output, exit: (code) => exits.push(code) });
  await new Promise((resolve) => setImmediate(resolve));
  socket.user = ME;
  socket.ev.emit("connection.update", { connection: "open" });
  input.write('this is not json\n{"id": 1, "cmd": "send", "to": "120363041234567890@g.us", "text": "On it"}\n');
  await new Promise((resolve) => setTimeout(resolve, 30));
  const lines = written.join("").trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(lines, [{ event: "state", state: "starting" },
    { event: "state", state: "open", me: { jid: "919800000001@s.whatsapp.net", name: "VK" } },
    { reply: 1, ok: true, id: "MSG1" }]);
  input.end();      // Felix went away
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual([socket.ended, exits], [true, [0]]);
});
