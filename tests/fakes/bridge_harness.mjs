// Runs the real bridge logic (felix/whatsapp/sidecar/bridge.js) over stdin/stdout with a
// scripted WhatsApp connection, so Felix's Python side can be tested against the actual
// protocol code without WhatsApp or the WhatsApp library.
import { EventEmitter } from "node:events";
import { appendFileSync } from "node:fs";

import { runStdio } from "../../felix/whatsapp/sidecar/bridge.js";

const ME = { id: "919800000001:7@s.whatsapp.net", lid: "55500011122233:7@lid", name: "VK" };
const RAVI = "919812345678@s.whatsapp.net";
// Tests run Felix on a fixed clock; HARNESS_NOW keeps this side's timestamps in the same era.
const now = () => Number(process.env.HARNESS_NOW) || Math.floor(Date.now() / 1000);
let sentCount = 0;

function connect() {
  const socket = {
    ev: new EventEmitter(),
    user: undefined,
    async sendMessage(jid, content) {
      sentCount += 1;
      const key = { id: `HAR${sentCount}`, remoteJid: jid, fromMe: true };
      if (process.env.HARNESS_LOG) appendFileSync(process.env.HARNESS_LOG, `${JSON.stringify({ to: jid, text: content.text })}\n`);
      // WhatsApp hands every linked device a copy of what the account sent.
      setTimeout(() => socket.ev.emit("messages.upsert", { type: "notify",
        messages: [{ key, message: { conversation: content.text }, messageTimestamp: now() }] }), 5);
      return { key };
    },
    async onWhatsApp(number) { return [{ jid: `${number}@s.whatsapp.net`, exists: !number.startsWith("000") }]; },
    async groupMetadata(jid) { return { id: jid, subject: "Leadership" }; },
    async requestPairingCode() { return "ABCD1234"; },
    async logout() {
      setTimeout(() => socket.ev.emit("connection.update", { connection: "close", lastDisconnect: { error: { output: { statusCode: 401 } } } }), 5);
    },
    async end() {},
  };
  const at = (ms, fn) => setTimeout(fn, ms);
  at(10, () => socket.ev.emit("connection.update", { qr: "2@harness-qr" }));
  at(60, () => { socket.user = ME; socket.ev.emit("connection.update", { connection: "open" }); });
  at(80, () => socket.ev.emit("contacts.upsert", [{ id: RAVI, name: "Ravi Kumar", notify: "Ravi K" }]));
  // A customer writes; a group is busy; the owner types a command to Felix in "Message yourself".
  at(120, () => socket.ev.emit("messages.upsert", { type: "notify", messages: [
    { key: { remoteJid: RAVI, id: "IN1", fromMe: false }, message: { conversation: "Need the contract signed today please" },
      pushName: "Ravi K", messageTimestamp: now() },
    { key: { remoteJid: "120363041234567890@g.us", id: "IN2", fromMe: false, participant: "919800011122@s.whatsapp.net" },
      message: { extendedTextMessage: { text: "Board deck draft 3 is out" } }, pushName: "Meera", messageTimestamp: now() },
    { key: { remoteJid: "status@broadcast", id: "IN3", fromMe: false }, message: { conversation: "a status update" }, messageTimestamp: now() }] }));
  at(200, () => socket.ev.emit("messages.upsert", { type: "notify", messages: [
    { key: { remoteJid: "55500011122233@lid", id: "CMD1", fromMe: true }, message: { conversation: "add task call the bank" },
      messageTimestamp: now() }] }));
  return socket;
}

runStdio({ connect, forget: async () => {} });
