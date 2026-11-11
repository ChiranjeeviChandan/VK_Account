/* Felix overlay: a small command bar that can float above other windows.
   Same rules as the dashboard: no dependencies, and nothing from mail or chats is ever written as HTML. */
"use strict";

const THREAD = "overlay";
const SUGGESTIONS = ["Brief me", "What's broken?", "Who is waiting on me?", "What's next?"];
// Look elements up inside the overlay itself: when it floats, it lives in another window's document.
const ROOT = document.getElementById("overlay");
const $ = (id) => ROOT.querySelector(`#${id}`);
let busy = false;
let chosenModel = "";      // the model picked in Settings, to notice when another one answered

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function button(label, className, onClick) {
  const node = make("button", className, label);
  node.type = "button";
  node.addEventListener("click", onClick);
  return node;
}
async function api(path, body) {
  const init = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const response = await fetch(path, init);
  if (response.status === 401) { location.reload(); throw new Error("signed out"); }
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || data.error || `Request failed (${response.status})`);
  return data;
}
function bubble(role, text) {
  const node = make("div", `msg ${role}`);
  if (role === "felix") window.felixRender(node, text || ""); else node.textContent = text || "";
  $("chat-log").append(node);
  node.scrollIntoView({ block: "end" });
  return node;
}
function approvalButtons(node, approvals) {
  if (!approvals || !approvals.length) return;
  const row = make("div", "mini");
  for (const approval of approvals) {
    const decide = async (verb) => {
      try {
        const result = await api(`/api/approvals/${approval.id}/${verb}`, {});
        row.replaceChildren(make("span", "hint", verb === "approve" ? result.message : "Cancelled."));
      } catch (error) { row.replaceChildren(make("span", "hint", error.message)); }
    };
    row.append(button(`Approve #${approval.id}`, "btn small primary", () => decide("approve")),
      button("Cancel", "btn small danger", () => decide("deny")));
  }
  node.append(row);
}

async function ask(text) {
  text = (text || "").trim();
  if (!text || busy) return;
  busy = true;
  $("chat-log").querySelector(".welcome")?.remove();
  bubble("user", text);
  const reply = bubble("felix", "");
  reply.classList.add("busy");
  let written = "";
  try {
    const response = await fetch("/api/chat/stream", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, thread: THREAD }) });
    if (!response.ok) throw new Error(`Felix could not answer (${response.status})`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const name = /^event: (.+)$/m.exec(block)?.[1];
        const raw = /^data: (.+)$/m.exec(block)?.[1];
        if (!name || !raw) continue;
        const data = JSON.parse(raw);
        if (name === "delta") { written += data.text; window.felixRender(reply, written); }
        else if (name === "tool" && data.status === "start" && !written) reply.textContent = `Checking ${data.name.replace(/_/g, " ")}`;
        else if (name === "done") {
          window.felixRender(reply, data.reply);
          if (data.model && chosenModel && data.model !== chosenModel) reply.append(make("span", "tool", `Answered by ${data.model}`));
          approvalButtons(reply, data.approvals);
        }
        else if (name === "error") reply.textContent = data.message;
        reply.scrollIntoView({ block: "end" });
      }
    }
  } catch (error) {
    reply.textContent = error.message;
  } finally {
    reply.classList.remove("busy");
    busy = false;
  }
}

async function refresh() {
  try {
    const status = await api("/api/status");
    chosenModel = status.brain.model;
    const engine = { claude: status.brain.model, local: "Local model", rules: "Commands" }[status.brain.engine] || status.brain.engine;
    $("engine").textContent = `${status.net.online ? "Online" : "Offline"} · ${engine}`;
  } catch { $("engine").textContent = "not reachable"; }
}

async function floatOnTop() {
  // Document Picture-in-Picture gives a small always-on-top window (Chrome and Edge).
  const root = ROOT;
  const home = root.parentElement;
  const mini = await window.documentPictureInPicture.requestWindow({ width: 420, height: 560 });
  for (const sheet of document.querySelectorAll('link[rel="stylesheet"]')) {
    const link = mini.document.createElement("link");
    link.rel = "stylesheet";
    link.href = sheet.href;
    mini.document.head.append(link);
  }
  mini.document.body.className = "overlay-page";
  mini.document.body.append(root);
  $("float").hidden = true;
  const note = make("p", "empty floated-note", "Felix is floating in its own small window. Close that window to bring it back here.");
  home.append(note);
  mini.addEventListener("pagehide", () => { note.remove(); home.append(root); $("float").hidden = false; });
  root.querySelector("#chat-input").focus();
}

function start() {
  const input = $("chat-input");
  $("chips").replaceChildren(...SUGGESTIONS.map((text) => button(text, "", () => ask(text))));
  const grow = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 120)}px`; };
  input.addEventListener("input", grow);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("chat-form").requestSubmit(); }
  });
  $("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value;
    input.value = ""; grow();
    ask(text);
  });
  $("chat-new").addEventListener("click", async () => {
    await api("/api/chat/reset", { thread: THREAD });
    $("chat-log").replaceChildren(make("p", "welcome", "Fresh conversation. What do you need?"));
  });
  if ("documentPictureInPicture" in window) {
    $("float").hidden = false;
    $("float").addEventListener("click", () => floatOnTop().catch((error) => bubble("felix", `Could not float: ${error.message}`)));
  }
  api(`/api/transcript?thread=${THREAD}&limit=12`).then((data) => {
    if (!data.turns.length) $("chat-log").append(make("p", "welcome", "Ask anything or give a command. Shift+Enter adds a line."));
    for (const turn of data.turns) bubble(turn.role === "user" ? "user" : "felix", turn.text);
  }).catch(() => {});
  refresh();
  setInterval(refresh, 30000);
}
start();
