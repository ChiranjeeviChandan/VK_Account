// Felix WhatsApp bridge: links to WhatsApp as a companion device (like WhatsApp Web)
// and speaks Felix's JSON-lines protocol on stdin/stdout. Felix starts and stops this
// process itself; see docs/WHATSAPP.md before turning it on.
import { rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import makeWASocket, { Browsers, fetchLatestWaWebVersion, useMultiFileAuthState } from "baileys";

import { runStdio } from "./bridge.js";

// stdout carries the protocol and nothing else: whatever a library prints goes to stderr.
console.log = console.info = console.debug = (...args) => console.error(...args);

const here = path.dirname(fileURLToPath(import.meta.url));
const session = process.env.FELIX_BRIDGE_SESSION || path.join(here, "session");
const silent = { level: "silent", child: () => silent, trace() {}, debug() {}, info() {}, warn() {}, error() {}, fatal() {} };

let version;      // WhatsApp refuses clients that announce a stale version, so ask for the current one once

async function connect() {
  if (version === undefined) {
    const latest = await fetchLatestWaWebVersion({ signal: AbortSignal.timeout(8000) }).catch(() => null);
    version = latest && !latest.error ? latest.version : null;
  }
  const { state, saveCreds } = await useMultiFileAuthState(session);
  const socket = makeWASocket({
    auth: state,
    logger: silent,
    ...(version ? { version } : {}),
    browser: Browsers.appropriate("Chrome"),
    // Stay "offline" as a client so the phone keeps ringing and notifying as usual.
    markOnlineOnConnect: false,
    syncFullHistory: false,
    generateHighQualityLinkPreview: false,
    shouldIgnoreJid: (jid) => typeof jid === "string" && (jid.endsWith("@broadcast") || jid.endsWith("@newsletter")),
  });
  socket.ev.on("creds.update", saveCreds);
  return socket;
}

const bridge = runStdio({ connect, forget: () => rm(session, { recursive: true, force: true }) });
process.on("uncaughtException", bridge.fail);
process.on("unhandledRejection", bridge.fail);
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => bridge.stop().finally(() => process.exit(0)));
