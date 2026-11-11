/* Felix dashboard. Plain JavaScript, no dependencies, no network beyond this laptop.
   Everything that comes from mail, chats or the web is written with textContent,
   never as HTML, so a hostile message cannot run code here. */
"use strict";

// ---------------------------------------------------------------- helpers
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "value") node.value = value;
    else if (key === "checked" || key === "disabled" || key === "selected" || key === "hidden") node[key] = Boolean(value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
const $ = (id) => document.getElementById(id);

async function api(path, options) {
  const init = { headers: {}, ...(options || {}) };
  if (init.body !== undefined && typeof init.body !== "string") {
    init.body = JSON.stringify(init.body);
    init.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, init);
  if (response.status === 401) { location.reload(); throw new Error("signed out"); }
  const data = response.headers.get("Content-Type")?.startsWith("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error((data && (data.message || data.error)) || `Request failed (${response.status})`);
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: body || {} });

let toastTimer = 0;
function toast(message) {
  const box = $("toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 4200);
}
async function act(work, done) {
  try { const result = await work(); if (done) toast(typeof done === "function" ? done(result) : done); return result; }
  catch (error) { toast(error.message); return null; }
}

function ago(value) {
  const then = typeof value === "number" ? value * 1000 : Date.parse(value);
  const seconds = (Date.now() - then) / 1000;
  if (!isFinite(seconds)) return "";
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  if (seconds < 7 * 86400) return `${Math.round(seconds / 86400)} d ago`;
  return new Date(then).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
// Felix sends times in the owner's time zone. Show that wall clock even if this browser's zone differs.
const wall = (iso) => new Date(`${iso.slice(0, 16)}Z`);
const clock = (iso) => wall(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", timeZone: "UTC" });
const dayName = (iso) => wall(iso).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "short", timeZone: "UTC" });
const dayTime = (iso) => wall(iso).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit", timeZone: "UTC" });
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function card(title, count, body, extra) {
  return el("section", { class: `card ${extra?.span || "span-6"}` },
    el("header", {}, el("h2", { text: title }), el("span", { class: "count", text: count ?? "" }), extra?.action),
    body);
}
const empty = (text) => el("p", { class: "empty", text });
const CATEGORY = { incident: "outage", security: "security", legal: "legal", churn: "customer at risk", complaint: "complaint",
  blocker: "blocked", payment: "payment", people: "people", alert: "system alert" };

// ---------------------------------------------------------------- state and navigation
const PAGES = [["today", "Today"], ["approvals", "Approvals"], ["inbox", "Inbox"], ["messages", "Messages"],
  ["calendar", "Calendar"], ["tasks", "To-do"], ["pulse", "Pulse"], ["settings", "Settings"]];
const state = { page: "today", status: null, inboxView: "inbox", emailId: null, chatId: null, pulseHours: 24, busy: false };

function navigate(page) {
  state.page = PAGES.some(([id]) => id === page) ? page : "today";
  if (location.hash.slice(1) !== state.page) history.replaceState(null, "", `#${state.page}`);
  drawNav();
  render();
}
function drawNav() {
  const counts = state.status?.counts || {};
  const badges = { approvals: counts.approvals, inbox: state.status?.mail?.unread, messages: counts.unread_chats };
  $("nav").replaceChildren(...PAGES.map(([id, label]) => el("button", {
    type: "button", "aria-current": state.page === id ? "page" : null, onclick: () => navigate(id) },
    el("span", { text: label }),
    badges[id] ? el("span", { class: id === "approvals" ? "badge" : "badge soft", text: badges[id] }) : null)));
}

async function refreshStatus() {
  try { state.status = await api("/api/status"); } catch { return; }
  const { net, brain, phone } = state.status;
  const engine = { claude: "Claude", local: "Local model", rules: "Commands" }[brain.engine] || brain.engine;
  $("net-label").textContent = `${net.online ? "Online" : "Offline"} · ${engine}`;
  $("net-pill").querySelector(".dot").className = `dot ${net.online ? "on" : "off"}`;
  for (const button of $("mode-menu").querySelectorAll("button")) button.setAttribute("aria-checked", String(button.dataset.mode === net.mode));
  $("phone-label").textContent = phone.connected ? "Phone connected" : phone.paired ? "Phone not reachable" : "No phone paired";
  $("phone-pill").querySelector(".dot").className = `dot ${phone.connected ? "on" : phone.paired ? "off" : ""}`;
  $("engine").textContent = brain.engine === "claude" ? brain.model : engine;
  const notice = $("notice");
  const lines = [];
  if (state.status.sandbox) lines.push(["This is the demo: sample data only, and nothing you approve here is really sent."]);
  if (net.mode === "offline") lines.push(["Offline mode is on: Felix will not touch the network. Anything you send waits in the queue."]);
  else if (brain.engine === "rules" && net.online && !brain.claude_ready) {
    lines.push(["Felix has no Claude API key, so it follows direct commands only. ",
      el("a", { href: "#settings", text: "Add a key in Settings", onclick: () => navigate("settings") }), " for the full assistant."]);
  }
  notice.hidden = !lines.length;
  notice.replaceChildren(...lines.map((parts) => el("div", {}, ...parts)));
  drawNav();
}

// ---------------------------------------------------------------- pages
const pages = {};
let renderToken = 0;
async function render() {
  const token = ++renderToken;
  const [, label] = PAGES.find(([id]) => id === state.page);
  $("page-title").textContent = label;
  $("page-sub").textContent = "";
  try {
    const node = await pages[state.page]();
    if (token === renderToken) $("page").replaceChildren(node);
  } catch (error) {
    if (token === renderToken) $("page").replaceChildren(empty(`Could not load this page: ${error.message}`));
  }
}


function issueRow(issue, withActions) {
  const level = issue.severity >= 0.75 ? "high" : issue.severity >= 0.5 ? "" : "low";
  return el("li", {},
    el("span", { class: `sev ${level}` }),
    el("div", { class: "grow" },
      el("div", { class: "title", text: issue.title }),
      el("div", { class: "meta", text: [issue.people.slice(0, 3).join(", "), issue.count > 1 ? plural(issue.count, "message") : "",
        ago(issue.last_ts)].filter(Boolean).join(" · ") }),
      el("div", { class: "snippet", text: issue.latest }),
      el("div", { class: "tags" },
        issue.categories.slice(0, 3).map((c) => el("span", { class: "tag bad", text: CATEGORY[c] || c })),
        issue.vip ? el("span", { class: "tag accent", text: "VIP" }) : null,
        issue.needs_reply ? el("span", { class: "tag warn", text: "awaiting your reply" }) : null,
        [...new Set(issue.sources.map((s) => s.split(":")[0]))].map((s) => el("span", { class: "tag", text: s }))),
      withActions ? el("div", { class: "row-actions" },
        el("button", { class: "btn small", type: "button", text: "Draft a reply",
          onclick: () => ask(`Draft a reply about "${issue.title}" and show it to me before sending.`) }),
        el("button", { class: "btn small ghost", type: "button", text: "Mark handled", onclick: async () => {
          await act(() => post("/api/pulse/dismiss", { signal_ids: issue.items.map((i) => i.signal_id) }), "Marked as handled");
          render();
        } })) : null));
}

function approvalCard(item, after) {
  const editable = ["body", "text", "content", "command"].find((key) => typeof item.args?.[key] === "string");
  const editor = el("textarea", { hidden: true, "aria-label": "Edit before approving" });
  const decide = async (yes) => {
    const body = yes && !editor.hidden && editable ? { args: { ...item.args, [editable]: editor.value } } : {};
    const result = await act(() => post(`/api/approvals/${item.id}/${yes ? "approve" : "deny"}`, body),
      (r) => (yes ? r.message : "Cancelled"));
    if (result) { await refreshStatus(); after(); }
  };
  return el("div", { class: "approval" },
    el("div", { class: "kind" }, el("span", { text: `#${item.id}` }),
      el("span", { class: `tag ${item.risk === "danger" ? "bad" : "warn"}`, text: item.risk === "danger" ? "changes this laptop or phone" : "sends as you" }),
      el("span", { class: "meta", text: ago(item.created_at) })),
    el("pre", { class: "pre preview", text: item.preview || item.tool }),
    editor,
    el("div", { class: "actions" },
      el("button", { class: "btn primary", type: "button", text: "Approve", onclick: () => decide(true) }),
      editable ? el("button", { class: "btn", type: "button", text: "Edit first", onclick: () => {
        editor.hidden = !editor.hidden;
        if (!editor.hidden) { editor.value = item.args[editable]; editor.focus(); }
      } }) : null,
      el("button", { class: "btn danger", type: "button", text: "Cancel it", onclick: () => decide(false) })));
}


function agendaList(day) {
  return el("ul", { class: "rows" }, day.events.map((e) => el("li", {},
    el("span", { class: "time-col", text: e.time }),
    el("div", { class: "grow" }, el("div", { class: "title", text: e.title }),
      el("div", { class: "meta", text: [e.location, (e.with || []).join(", ")].filter(Boolean).join(" · ") })))));
}

function setupCard(status) {
  const setup = status.setup;
  const steps = [
    [setup.claude, "Add your Claude API key", "So Felix understands anything you ask. Without it, it follows direct commands only."],
    [setup.mail, "Connect your email", "Felix reads, summarises and drafts replies. Nothing is sent without your approval."],
    [setup.phone, "Pair your Android phone", "For WhatsApp, calls, texts and phone control, at home or on the move."],
    [setup.whatsapp_number, "Add your WhatsApp number", "Where the morning and evening briefs are sent."],
  ];
  if (status.sandbox || setup.dismissed || steps.every(([done]) => done)) return null;
  const left = steps.filter(([done]) => !done).length;
  return card("Finish setting up Felix", `${left} to go`, el("div", {},
    el("ol", { class: "steps" }, steps.map(([done, title, why], index) => el("li", { class: done ? "done" : "" },
      el("span", { class: done ? "mark-done" : "mark-todo", text: done ? "✓" : String(index + 1) }),
      el("div", { class: "grow" }, el("div", { class: "title", text: title }), done ? null : el("div", { class: "meta", text: why })),
      done ? null : el("button", { class: "btn small", type: "button", text: "Open Settings", onclick: () => navigate("settings") })))),
    el("button", { class: "more", type: "button", text: "Hide this", onclick: async () => {
      await act(() => post("/api/settings", { ui: { setup_dismissed: true } })); render();
    } })));
}

pages.today = async () => {
  const [today, approvals, status] = await Promise.all([api("/api/today"), api("/api/approvals"), api("/api/status")]);
  const facts = today.facts;
  $("page-sub").textContent = `${facts.greeting}${facts.owner ? `, ${facts.owner}` : ""} · ${facts.date}`;
  const due = facts.tasks.overdue.length + facts.tasks.today.length;
  const stats = el("div", { class: "stats" },
    stat(facts.broken.length, "broken", facts.broken.length > 0),
    stat(facts.waiting_on_you.length, "waiting on you"),
    stat(facts.day.count, facts.day.count === 1 ? "meeting left today" : "meetings left today"),
    stat(due, due === 1 ? "to-do due" : "to-dos due", facts.tasks.overdue.length > 0),
    stat(facts.inbox.unread + facts.chats.unread, "unread"));
  const left = el("div", { class: "col" });
  const right = el("div", { class: "col" });

  const setup = setupCard(status);
  if (setup) left.append(setup);
  if (approvals.approvals.length) {
    left.append(card("Waiting for your approval", plural(approvals.approvals.length, "action"),
      el("div", { class: "body flush" }, approvals.approvals.map((a) => approvalCard(a, render)))));
  }
  left.append(card("What's broken", facts.broken.length ? String(facts.broken.length) : "",
    facts.broken.length ? el("ul", { class: "rows" }, facts.broken.map((i) => issueRow(i, true)))
      : el("div", { class: "body" }, empty("Nothing looks broken in the last 24 hours."))));

  const waiting = facts.waiting_on_you;
  left.append(card("Waiting on you", waiting.length ? String(waiting.length) : "",
    waiting.length ? el("div", {}, el("ul", { class: "rows" }, waiting.slice(0, 6).map((w) => el("li", { class: "clickable", onclick: () => {
      if (w.kind === "email") { state.emailId = w.ref_id; state.inboxView = "inbox"; navigate("inbox"); } else navigate("messages");
    } },
      el("div", { class: "grow" },
        el("div", { class: "title", text: w.who }),
        el("div", { class: "snippet", text: w.title }),
        el("div", { class: "tags" }, w.vip ? el("span", { class: "tag accent", text: "VIP" }) : null,
          w.urgent ? el("span", { class: "tag bad", text: "urgent" }) : null,
          el("span", { class: "tag", text: w.kind === "email" ? "email" : w.source }))),
      el("span", { class: "when", text: ago(w.ts) })))),
      waiting.length > 6 ? el("button", { class: "more", type: "button", text: `See all ${waiting.length} in Needs reply`,
        onclick: () => { state.inboxView = "waiting"; navigate("inbox"); } }) : null)
      : el("div", { class: "body" }, empty("Nobody is waiting on you."))));

  const day = facts.day.events.length ? facts.day : today.tomorrow;
  const dayTitle = facts.day.events.length ? "Rest of today" : "Tomorrow";
  right.append(card(dayTitle, day.count ? plural(day.count, "meeting") : "", el("div", { class: "body flush" },
    facts.day.events.length ? null : el("div", { class: "body" }, empty("No more meetings today.")),
    day.events.length ? agendaList(day) : (facts.day.events.length ? null : el("div", { class: "body" }, empty("Nothing booked tomorrow either."))),
    day.overlaps.map((c) => el("div", { class: "body" }, el("span", { class: "tag warn", text: `Clash: ${c}` }))))));

  const todo = [...facts.tasks.overdue.map((t) => ({ ...t, overdue: true })), ...facts.tasks.today, ...facts.tasks.important];
  right.append(card("To do", due ? `${due} due` : "", el("div", { class: "body flush" },
    todo.length ? el("ul", { class: "rows" }, todo.slice(0, 8).map((t) => taskRow(t, render)))
      : el("div", { class: "body" }, empty("Nothing due today.")),
    facts.tasks.suggested ? el("button", { class: "more", type: "button", text: `${plural(facts.tasks.suggested, "suggestion")} from your messages`,
      onclick: () => navigate("tasks") }) : null,
    addTaskForm(render))));

  if (facts.wins.length) {
    right.append(card("Good news", "", el("ul", { class: "rows" }, facts.wins.map((w) => el("li", {},
      el("div", { class: "grow" }, el("div", { class: "title", text: w.title }), el("div", { class: "meta", text: w.who || "" })))))));
  }

  right.append(card("Your brief", "", el("div", { class: "body" },
    el("pre", { class: "pre brief", text: today.text }),
    el("div", { class: "tags" },
      el("button", { class: "btn small", type: "button", text: "Send to my WhatsApp", onclick: () =>
        act(() => post("/api/brief/send", { kind: "now" }), (r) => (r.via ? `Sent via ${r.via}` : `Not delivered: ${r.errors[0] || "no route is connected"}`)) })))));
  return el("div", {}, stats, el("div", { class: "cols" }, left, right));
};

function stat(n, label, alert) {
  return el("div", { class: `stat ${alert ? "alert" : ""}` }, el("div", { class: "n", text: n }), el("div", { class: "l", text: label }));
}
function taskRow(task, after) {
  return el("li", {},
    el("button", { class: "check", type: "button", title: "Mark done", "aria-label": `Mark done: ${task.title}`, onclick: async () => {
      await act(() => post(`/api/tasks/${task.id}`, { action: "done" }), `Done: ${task.title}`); after();
    } }),
    el("div", { class: "grow" },
      el("div", { class: "title", text: task.title }),
      el("div", { class: "tags" },
        task.overdue ? el("span", { class: "tag bad", text: "overdue" }) : null,
        task.due ? el("span", { class: "tag", text: `due ${dayTime(task.due)}` }) : null,
        task.high || task.priority === "high" ? el("span", { class: "tag warn", text: "high" }) : null)));
}
function addTaskForm(after) {
  const input = el("input", { type: "text", placeholder: "Add a to-do, e.g. Call the bank tomorrow 5pm", "aria-label": "New to-do" });
  return el("form", { class: "inline-form", onsubmit: async (event) => {
    event.preventDefault();
    if (!input.value.trim()) return;
    const result = await act(() => post("/api/chat", { text: `add task ${input.value.trim()}`, thread: "quick" }));
    if (result) { toast(result.reply); input.value = ""; after(); }
  } }, input, el("button", { class: "btn", type: "submit", text: "Add" }));
}

pages.approvals = async () => {
  const [pending, recent] = await Promise.all([api("/api/approvals"), api("/api/approvals?status=all&limit=15")]);
  $("page-sub").textContent = "Nothing is sent in your name, and nothing risky runs, until you say so.";
  const done = recent.approvals.filter((a) => a.status !== "pending");
  return el("div", { class: "grid" },
    card("Waiting for you", pending.approvals.length ? String(pending.approvals.length) : "",
      pending.approvals.length ? el("div", { class: "body flush" }, pending.approvals.map((a) => approvalCard(a, render)))
        : el("div", { class: "body" }, empty("Nothing is waiting.")), { span: "span-7" }),
    card("Recently decided", "", done.length ? el("ul", { class: "rows" }, done.map((a) => el("li", {},
      el("div", { class: "grow" }, el("div", { class: "title", text: (a.preview || a.tool).split("\n")[0] }),
        el("div", { class: "meta", text: (a.preview || "").split("\n").slice(1, 3).join(" · ") })),
      el("span", { class: `tag ${a.status === "executed" ? "good" : a.status === "failed" ? "bad" : ""}`, text: a.status }))))
      : el("div", { class: "body" }, empty("No history yet.")), { span: "span-5" }));
};

pages.inbox = async () => {
  const query = state.inboxQuery ? `&q=${encodeURIComponent(state.inboxQuery)}` : "";
  const data = await api(`/api/inbox?view=${state.inboxView}${query}`);
  $("page-sub").textContent = data.accounts || data.emails.length ? `${data.unread} unread`
    : "No email account is connected yet. Add one in Settings.";
  const search = el("input", { type: "search", placeholder: "Search mail", value: state.inboxQuery || "", "aria-label": "Search mail" });
  search.addEventListener("change", () => { state.inboxQuery = search.value.trim(); render(); });
  const tabs = el("div", { class: "tabs", role: "tablist" }, [["inbox", "Inbox"], ["unread", "Unread"], ["waiting", "Needs reply"], ["sent", "Sent"]]
    .map(([id, label]) => el("button", { type: "button", role: "tab", "aria-selected": String(state.inboxView === id), text: label,
      onclick: () => { state.inboxView = id; state.emailId = null; render(); } })));
  const reader = el("div", { class: "card reader" }, el("div", { class: "body" }, empty("Select an email to read it.")));
  const list = el("ul", { class: "rows" }, data.emails.map((mail) => el("li", {
    class: `clickable ${mail.unread ? "unread" : ""} ${state.emailId === mail.id ? "selected" : ""}`,
    onclick: () => { state.emailId = mail.id; openEmail(mail.id, reader); } },
    el("div", { class: "grow" },
      el("div", { class: "title", text: mail.sent_by_owner ? `To ${(mail.to || []).join(", ")}` : mail.from }),
      el("div", { text: mail.subject }),
      el("div", { class: "snippet", text: mail.snippet }),
      el("div", { class: "tags" }, mail.vip ? el("span", { class: "tag accent", text: "VIP" }) : null,
        mail.needs_reply ? el("span", { class: "tag warn", text: "needs reply" }) : null,
        mail.urgent ? el("span", { class: "tag bad", text: "urgent" }) : null,
        (mail.flagged_as || []).map((c) => el("span", { class: "tag bad", text: CATEGORY[c] || c })),
        mail.attachments ? el("span", { class: "tag", text: plural(mail.attachments.length, "attachment") }) : null)),
    el("span", { class: "when", text: ago(mail.date) }))));
  if (state.emailId) openEmail(state.emailId, reader);
  return el("div", {}, el("div", { class: "toolbar" }, tabs, search),
    el("div", { class: "split" }, el("div", { class: "card" }, data.emails.length ? list : el("div", { class: "body" }, empty("No emails here."))), reader));
};
async function openEmail(id, reader) {
  let mail;
  try { mail = await api(`/api/emails/${id}`); } catch (error) { reader.replaceChildren(el("div", { class: "body" }, empty(error.message))); return; }
  const reply = el("textarea", { placeholder: "Write a reply. It is sent exactly as you type it.", "aria-label": "Reply" });
  const send = async (all) => {
    if (!reply.value.trim()) return;
    const result = await act(() => post(`/api/emails/${id}/reply`, { body: reply.value, reply_all: all }), (r) => r.message);
    if (result) { reply.value = ""; refreshStatus(); }
  };
  const action = (name, label) => el("button", { class: "btn small ghost", type: "button", text: label, onclick: async () => {
    const result = await act(() => post(`/api/emails/${id}/${name}`), (r) => r.message);
    if (result && (name === "archive" || name === "trash")) { state.emailId = null; render(); }
  } });
  reader.replaceChildren(
    el("div", { class: "head" }, el("h2", { text: mail.subject }),
      el("div", { class: "meta", text: `${mail.sent_by_owner ? "You" : `${mail.from} <${mail.from_address}>`} · ${dayTime(mail.date)}` }),
      mail.attachments ? el("div", { class: "tags" }, mail.attachments.map((a) => el("span", { class: "tag", text: a }))) : null),
    el("pre", { class: "pre text", text: mail.body }),
    el("div", { class: "reply" }, reply,
      el("div", { class: "actions" },
        el("button", { class: "btn primary", type: "button", text: "Send reply", onclick: () => send(false) }),
        mail.cc ? el("button", { class: "btn", type: "button", text: "Reply all", onclick: () => send(true) }) : null,
        el("button", { class: "btn", type: "button", text: "Ask Felix to draft", onclick: () => ask(`Draft a reply to email ${id}. Show me the draft before sending.`) }),
        action("archive", "Archive"), action("unread", "Mark unread"))));
}

pages.messages = async () => {
  const data = await api("/api/chats?channel=all");
  const routes = data.routes;
  const ready = routes.bridge.ready || routes.phone.ready;
  $("page-sub").textContent = ready ? `${data.unread} unread` : "WhatsApp is not connected right now — pair your phone in Settings to read and reply.";
  const thread = el("div", { class: "card reader" }, el("div", { class: "body" }, empty("Select a chat.")));
  const open = async (chat) => {
    state.chatId = chat.id;
    const detail = await api(`/api/chats/${chat.id}`);
    const box = el("textarea", { placeholder: `Message ${detail.chat.chat}`, rows: 2, "aria-label": "Message" });
    const bubbles = el("div", { class: "bubbles" }, detail.messages.map((m) => el("div", { class: `bubble ${m.mine ? "mine" : ""}` },
      el("span", { class: "who", text: `${m.mine ? "You" : m.from} · ${clock(m.when)}` }), m.text)));
    thread.replaceChildren(
      el("div", { class: "head" }, el("h2", { text: detail.chat.chat }),
        el("div", { class: "meta", text: [detail.chat.group ? "group" : "", detail.chat.channel || "whatsapp"].filter(Boolean).join(" · ") })),
      bubbles,
      el("div", { class: "reply" }, box, el("div", { class: "actions" },
        el("button", { class: "btn primary", type: "button", text: "Send", onclick: async () => {
          if (!box.value.trim()) return;
          const result = await act(() => post(`/api/chats/${chat.id}/send`, { text: box.value }), (r) => r.message);
          if (result) { box.value = ""; open(chat); }
        } }),
        el("button", { class: "btn", type: "button", text: "Ask Felix to draft", onclick: () => ask(`Draft a WhatsApp reply to ${detail.chat.chat}. Show me the draft first.`) }))));
    bubbles.scrollTop = bubbles.scrollHeight;
    refreshStatus();
  };
  const list = el("ul", { class: "rows" }, data.chats.map((chat) => el("li", { class: `clickable ${chat.unread ? "unread" : ""}`, onclick: () => open(chat) },
    el("div", { class: "grow" }, el("div", { class: "title", text: chat.chat }),
      el("div", { class: "snippet", text: `${chat.last_from === "owner" ? "You: " : ""}${chat.last_message || ""}` }),
      el("div", { class: "tags" }, chat.needs_reply ? el("span", { class: "tag warn", text: "needs reply" }) : null,
        chat.looks_urgent_or_unhappy ? el("span", { class: "tag bad", text: "urgent or unhappy" }) : null,
        chat.group ? el("span", { class: "tag", text: "group" }) : null, chat.channel ? el("span", { class: "tag", text: chat.channel }) : null)),
    el("div", { class: "when" }, chat.unread ? el("span", { class: "badge soft", text: chat.unread }) : ago(chat.when)))));
  const current = data.chats.find((c) => c.id === state.chatId);
  if (current) open(current);
  return el("div", { class: "split" }, el("div", { class: "card" }, data.chats.length ? list : el("div", { class: "body" }, empty("No messages yet."))), thread);
};


pages.calendar = async () => {
  const data = await api(`/api/calendar?range=${encodeURIComponent("next 14 days")}`);
  $("page-sub").textContent = `Next 14 days · ${data.sources.map((s) => s.name).join(", ")}`;
  const days = new Map();
  for (const event of data.events) {
    const key = dayName(event.start);
    if (!days.has(key)) days.set(key, []);
    days.get(key).push(event);
  }
  const body = el("div", { class: "body flush" });
  for (const [day, events] of days) {
    // Two timed meetings clash when one starts before an earlier one has ended.
    let busyUntil = "";
    const clashes = new Set();
    for (const event of events) {
      if (event.all_day) continue;
      if (busyUntil && event.start < busyUntil) clashes.add(event.id);
      if (event.end > busyUntil) busyUntil = event.end;
    }
    body.append(el("div", { class: "day-head", text: day }), el("ul", { class: "rows" }, events.map((event) => el("li", {},
      el("span", { class: "time-col wide", text: event.all_day ? "All day" : `${clock(event.start)} – ${clock(event.end)}` }),
      el("div", { class: "grow" }, el("div", { class: "title", text: event.title }),
        el("div", { class: "meta", text: [event.location, (event.attendees || []).join(", "), event.repeats].filter(Boolean).join(" · ") }),
        clashes.has(event.id) || event.awaiting_your_rsvp ? el("div", { class: "tags" },
          clashes.has(event.id) ? el("span", { class: "tag warn", text: "overlaps the meeting before" }) : null,
          event.awaiting_your_rsvp ? el("span", { class: "tag warn", text: "you have not answered" }) : null) : null),
      el("div", { class: "row-actions" }, el("button", { class: "btn small ghost", type: "button", text: "Remove", onclick: async () => {
        if (await act(() => post("/api/calendar/delete", { event: event.id }), (r) => r.message)) render();
      } }))))));
  }
  if (!data.events.length) body.append(el("div", { class: "body" }, empty("Nothing scheduled in the next 14 days.")));
  const title = el("input", { type: "text", placeholder: "Board review", "aria-label": "Title" });
  const when = el("input", { type: "text", placeholder: "tomorrow 3pm for 45 minutes", "aria-label": "When" });
  const form = el("form", { class: "inline-form", onsubmit: async (event) => {
    event.preventDefault();
    if (!title.value.trim() || !when.value.trim()) return;
    const result = await act(() => post("/api/calendar", { title: title.value, start: when.value }), (r) => r.message);
    if (result) render();
  } }, title, when, el("button", { class: "btn primary", type: "submit", text: "Add" }));
  return el("div", { class: "grid" }, card("Schedule", plural(data.events.length, "event"), el("div", {}, body, form), { span: "span-12" }));
};


pages.tasks = async () => {
  const data = await api("/api/tasks");
  const open = data.overdue.length + data.today.length + data.upcoming.length + data.someday.length;
  $("page-sub").textContent = `${open} open`;
  const section = (label, items, flag) => (items.length ? [el("div", { class: "day-head", text: label }),
    el("ul", { class: "rows" }, items.map((t) => taskRow({ ...t, overdue: flag }, render)))] : []);
  const list = el("div", { class: "body flush" }, section("Overdue", data.overdue, true), section("Due today", data.today),
    section("Coming up", data.upcoming), section("No date", data.someday), open ? null : el("div", { class: "body" }, empty("Your list is empty.")),
    addTaskForm(render));
  const change = async (task, action) => { if (await act(() => post(`/api/tasks/${task.id}`, { action }))) render(); };
  const suggested = data.suggested.length ? el("ul", { class: "rows" }, data.suggested.map((t) => el("li", {},
    el("div", { class: "grow" }, el("div", { class: "title", text: t.title }), el("div", { class: "snippet", text: t.notes || "" }),
      el("div", { class: "row-actions" },
        el("button", { class: "btn small", type: "button", text: "Add to my list", onclick: () => change(t, "accept") }),
        el("button", { class: "btn small ghost", type: "button", text: "Dismiss", onclick: () => change(t, "delete") }))))))
    : el("div", { class: "body" }, empty("Requests Felix notices in your mail and messages are listed here."));
  const done = data.done.length ? el("ul", { class: "rows" }, data.done.slice(-6).reverse().map((t) => el("li", {},
    el("div", { class: "grow" }, el("div", { class: "meta", text: t.title })),
    el("button", { class: "btn small ghost", type: "button", text: "Reopen", onclick: () => change(t, "reopen") })))) : null;
  return el("div", { class: "cols" },
    el("div", { class: "col" }, card("My list", "", list), done ? card("Recently done", "", done) : null),
    el("div", { class: "col" }, card("Suggested from your messages", data.suggested.length ? String(data.suggested.length) : "", suggested)));
};


pages.pulse = async () => {
  const data = await api(`/api/pulse?hours=${state.pulseHours}`);
  const mood = data.mood;
  const span = { 24: "24 hours", 72: "3 days", 168: "7 days" }[state.pulseHours];
  $("page-sub").textContent = `${plural(data.counts.emails, "email")} and ${plural(data.counts.messages, "message")} in the last ${span} · mood ${mood.label}`
    + (Math.abs(mood.trend) >= 0.15 ? ` (${mood.trend > 0 ? "improving" : "worsening"})` : "");
  const tabs = el("div", { class: "tabs", role: "tablist" }, [[24, "24 hours"], [72, "3 days"], [168, "7 days"]].map(([hours, label]) =>
    el("button", { type: "button", role: "tab", "aria-selected": String(state.pulseHours === hours), text: label, onclick: () => { state.pulseHours = hours; render(); } })));
  const simple = (items, text) => (items.length ? el("ul", { class: "rows" }, items) : el("div", { class: "body" }, empty(text)));
  const toneTag = (value) => el("span", { class: `tag ${value <= -0.3 ? "bad" : value >= 0.3 ? "good" : ""}`,
    text: value <= -0.3 ? "negative" : value >= 0.3 ? "positive" : "neutral" });
  return el("div", {}, el("div", { class: "toolbar" }, tabs),
    el("div", { class: "cols" },
      el("div", { class: "col" },
        card("What's broken", data.broken.length ? String(data.broken.length) : "",
          simple(data.broken.map((i) => issueRow(i, true)), "Nothing looks broken.")),
        card("What's happening", "", simple(data.happening.map((i) => el("li", {},
          el("div", { class: "grow" }, el("div", { class: "title", text: i.title }),
            el("div", { class: "meta", text: [i.people.slice(0, 3).join(", "), i.count > 1 ? plural(i.count, "message") : ""].filter(Boolean).join(" · ") }),
            el("div", { class: "snippet", text: i.latest })), el("span", { class: "when", text: ago(i.last_ts) }))), "Quiet."))),
      el("div", { class: "col" },
        card("Whose tone to watch", "", simple(data.people.map((p) => el("li", {},
          el("div", { class: "grow" }, el("div", { class: "title", text: p.who }), el("div", { class: "meta", text: plural(p.count, "message") })),
          el("div", { class: "tags" }, p.vip ? el("span", { class: "tag accent", text: "VIP" }) : null,
            p.tone ? el("span", { class: `tag ${p.tone === "worsening" ? "bad" : "good"}`, text: p.tone }) : null, toneTag(p.sentiment)))), "No one stands out.")),
        card("Good news and recoveries", "", simple([
          ...data.wins.map((w) => el("li", {}, el("div", { class: "grow" }, el("div", { class: "title", text: w.title }), el("div", { class: "meta", text: w.who || "" })), el("span", { class: "tag good", text: "win" }))),
          ...data.recovered.map((r) => el("li", {}, el("div", { class: "grow" }, el("div", { class: "title", text: r.title })), el("span", { class: "tag good", text: "resolved" })))],
          "Nothing yet.")),
        data.news.length ? card("Watched news", "", simple(data.news.map((n) => el("li", {},
          el("div", { class: "grow" }, el("div", { class: "title", text: n.title }), el("div", { class: "meta", text: n.source })),
          el("span", { class: `tag ${n.tone === "negative" ? "bad" : n.tone === "positive" ? "good" : ""}`, text: n.tone }))), "")) : null)));
};

// ---------------------------------------------------------------- settings
function field(label, input, hint) {
  return el("div", {}, el("label", { text: label }), input, hint ? el("div", { class: "hint", text: hint }) : null);
}
function saveButton(collect) {
  return el("button", { class: "btn primary", type: "button", text: "Save", onclick: async () => {
    if (await act(() => post("/api/settings", collect()), "Saved")) refreshStatus();
  } });
}
const AUTONOMY = [["email", "Send email"], ["whatsapp", "Send WhatsApp"], ["sms", "Send SMS"], ["call", "Place calls"],
  ["calendar_invite", "Invite or notify meeting guests"], ["calendar", "Change my own calendar"], ["files_write", "Write files"],
  ["files_delete", "Delete files"], ["shell", "Run shell commands"], ["power", "Sleep or power off"],
  ["phone_ui", "Tap and type on my phone"], ["phone_control", "Phone switches (torch, volume...)"], ["laptop_control", "Laptop switches (volume, lock...)"]];
const LOCKED = new Set(["shell", "files_delete", "power"]);

pages.settings = async () => {
  const [settings, devices, audit, status] = await Promise.all([api("/api/settings"), api("/api/devices"),
    api("/api/audit?limit=25"), api("/api/status")]);
  const c = settings.config;
  $("page-sub").textContent = `Everything is stored on this laptop in ${settings.data_folder}. Secrets are kept in ${settings.secret_store === "keyring" ? "your system keychain" : "a private file there"}.`;
  const left = el("div", { class: "col" });
  const right = el("div", { class: "col" });

  const name = el("input", { type: "text", value: c.owner.name }), mail = el("input", { type: "email", value: c.owner.email });
  const phone = el("input", { type: "text", value: c.owner.phone, placeholder: "+919812345678" });
  const zone = el("input", { type: "text", value: c.owner.timezone, placeholder: "Asia/Kolkata" });
  const vips = el("input", { type: "text", value: (c.owner.vips || []).join(", "), placeholder: "anita@bigretail.com, board.org" });
  left.append(card("You", "", el("div", { class: "body" }, el("div", { class: "form-grid" },
    field("First name", name), field("Email", mail), field("WhatsApp number", phone, "With country code. Briefs are sent here."),
    field("Time zone", zone, "Empty uses this laptop's zone."), el("div", { class: "wide" }, field("VIPs", vips, "People or domains whose messages always rank first.")),
    el("div", { class: "wide" }, saveButton(() => ({ owner: { name: name.value.trim(), email: mail.value.trim(), phone: phone.value.trim(),
      timezone: zone.value.trim(), vips: vips.value.split(",").map((v) => v.trim()).filter(Boolean) } })))))));

  const mOn = el("input", { type: "checkbox", checked: c.briefs.morning.enabled }), mAt = el("input", { type: "time", value: c.briefs.morning.time });
  const eOn = el("input", { type: "checkbox", checked: c.briefs.evening.enabled }), eAt = el("input", { type: "time", value: c.briefs.evening.time });
  const weekends = el("input", { type: "checkbox", checked: c.briefs.weekends });
  const channels = el("input", { type: "text", value: c.briefs.channels.join(", ") });
  right.append(card("Morning and evening briefs", "", el("div", { class: "body" },
    el("div", { class: "field-row" }, el("label", {}, mOn, " Morning brief"), mAt),
    el("div", { class: "field-row" }, el("label", {}, eOn, " Evening wrap-up"), eAt),
    el("div", { class: "field-row" }, el("label", {}, weekends, " Also on weekends"), el("span", { class: "hint", text: "A brief missed while the laptop slept is sent on wake-up." })),
    field("Delivery order", channels, "First route that works wins: whatsapp, phone, email, desktop."),
    el("div", { class: "tags" }, saveButton(() => ({ briefs: { morning: { enabled: mOn.checked, time: mAt.value }, evening: { enabled: eOn.checked, time: eAt.value },
      weekends: weekends.checked, channels: channels.value.split(",").map((v) => v.trim()).filter(Boolean) } }))))));

  const key = el("input", { type: "password", placeholder: settings.secrets.anthropic_api_key ? "A key is saved. Paste a new one to replace it." : "sk-ant-...", autocomplete: "off" });
  const model = el("input", { type: "text", value: c.brain.model, list: "model-ids", spellcheck: "false" });
  const modelIds = el("datalist", { id: "model-ids" }, ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"].map((m) => el("option", { value: m })));
  const effort = el("select", {}, ["low", "medium", "high", "xhigh", "max"].map((e) => el("option", { value: e, text: e, selected: c.brain.effort === e })));
  const fast = el("input", { type: "checkbox", checked: c.brain.fast }), web = el("input", { type: "checkbox", checked: c.brain.web_search });
  const share = el("input", { type: "checkbox", checked: c.brain.share_bodies_with_cloud });
  const localModel = el("input", { type: "text", value: c.brain.local.model });
  left.append(card("Brain", "", el("div", { class: "body" },
    field("Claude API key", key, "Stored in your keychain or a private file; never shown again."),
    el("div", { class: "tags" }, el("button", { class: "btn", type: "button", text: "Save key", onclick: async () => {
      if (!key.value.trim()) return;
      if (await act(() => post("/api/secrets", { name: "anthropic_api_key", value: key.value }), "Key saved")) { key.value = ""; refreshStatus(); render(); }
    } }), settings.secrets.anthropic_api_key ? el("button", { class: "btn danger", type: "button", text: "Remove key", onclick: async () => {
      if (await act(() => post("/api/secrets", { name: "anthropic_api_key", value: "" }), "Key removed")) { refreshStatus(); render(); }
    } }) : null),
    el("div", { class: "form-grid", style: "margin-top:12px" }, field("Model", el("div", {}, model, modelIds), "Any Claude model your key can use."), field("Effort", effort, "Low answers fastest."),
      field("Offline model (Ollama)", localModel, "Optional. Used when there is no internet.")),
    el("div", { class: "field-row" }, el("label", {}, fast, " Fast mode"), el("span", { class: "hint", text: "Quicker replies at a higher price, on models that offer it." })),
    el("div", { class: "field-row" }, el("label", {}, web, " Web search"), el("span", { class: "hint", text: "Lets Claude look up current information." })),
    el("div", { class: "field-row" }, el("label", {}, share, " Send full message text to Claude"), el("span", { class: "hint", text: "Off: only short excerpts leave this laptop." })),
    el("div", { class: "tags" }, saveButton(() => ({ brain: { model: model.value.trim() || c.brain.model, effort: effort.value, fast: fast.checked, web_search: web.checked,
      share_bodies_with_cloud: share.checked, local: { model: localModel.value.trim() } } }))))));

  const selects = {};
  right.append(card("What Felix may do without asking", "", el("div", { class: "body" },
    AUTONOMY.map(([id, label]) => {
      selects[id] = el("select", { "aria-label": label }, [["ask", "Ask me first"], ["auto", "Do it"], ["never", "Never"]].map(([v, text]) =>
        el("option", { value: v, text: LOCKED.has(id) && v === "auto" ? "Do it (still asks)" : text, selected: (c.policy.autonomy[id] || "ask") === v })));
      return el("div", { class: "field-row" }, el("span", { text: label }), selects[id]);
    }),
    el("p", { class: "hint", text: "Shell commands, deletions and power actions always ask, whatever is chosen here." }),
    el("div", { class: "tags" }, saveButton(() => ({ policy: { autonomy: Object.fromEntries(Object.entries(selects).map(([id, s]) => [id, s.value])) } }))))));

  // accounts
  const accountRows = (c.mail.accounts || []).map((a) => el("div", { class: "field-row" },
    el("div", {}, el("div", { class: "title", text: a.email }), el("div", { class: "meta", text: `${a.imap_host} · ${a.auth || "password"}${a.has_password || a.auth !== "password" ? "" : " · no password saved"}` })),
    el("button", { class: "btn small danger", type: "button", text: "Remove", onclick: async () => {
      if (await act(() => api(`/api/mail/accounts/${encodeURIComponent(a.id)}`, { method: "DELETE" }), "Removed")) render();
    } })));
  const aMail = el("input", { type: "email", placeholder: "you@company.com" }), aPass = el("input", { type: "password", placeholder: "App password", autocomplete: "off" });
  const aImap = el("input", { type: "text", placeholder: "imap.company.com" });
  const aSmtp = el("input", { type: "text", placeholder: "smtp.company.com" });
  left.append(card("Email accounts", "", el("div", { class: "body" }, accountRows.length ? accountRows : empty("No account yet."),
    el("div", { class: "form-grid", style: "margin-top:12px" }, field("Address", aMail), field("Password", aPass, "Gmail needs an app password."),
      field("Incoming server", aImap, "Leave both empty for Gmail, Outlook, Yahoo or iCloud."), field("Outgoing server", aSmtp)),
    el("div", { class: "tags" }, el("button", { class: "btn primary", type: "button", text: "Connect", onclick: async () => {
      const body = { email: aMail.value.trim(), password: aPass.value };
      if (aImap.value.trim()) body.imap_host = aImap.value.trim();
      if (aSmtp.value.trim()) body.smtp_host = aSmtp.value.trim();
      const result = await act(() => post("/api/mail/accounts", body));
      if (!result) return;
      toast(result.check.ok ? "Connected" : result.check.ok === null ? "Saved (offline, not tested)" : `Saved, but: ${result.check.imap !== "ok" ? result.check.imap : result.check.smtp}`);
      render();
    } })))));

  const sources = c.calendar.sources.map((s) => el("div", { class: "field-row" },
    el("div", {}, el("div", { class: "title", text: s.name || s.id }), el("div", { class: "meta", text: s.kind + (s.url ? ` · ${s.url}` : "") })),
    s.kind === "local" ? null : el("button", { class: "btn small danger", type: "button", text: "Remove", onclick: async () => {
      if (await act(() => api(`/api/calendar/sources/${encodeURIComponent(s.id)}`, { method: "DELETE" }), "Removed")) render();
    } })));
  const ics = el("input", { type: "text", placeholder: "https://.../basic.ics" });
  right.append(card("Calendars", "", el("div", { class: "body" }, sources,
    field("Add a calendar by its address (ICS)", ics, "Read-only. For two-way Google Calendar run: felix connect google. Phone calendars appear after pairing."),
    el("div", { class: "tags" },
      el("button", { class: "btn", type: "button", text: "Add calendar", onclick: async () => {
        if (!ics.value.trim()) return;
        const result = await act(() => post("/api/calendar/sources", { kind: "ics", url: ics.value.trim(), name: "Calendar feed" }));
        if (result) { toast(result.sync.errors.length ? result.sync.errors[0] : `${result.sync.events} events`); render(); }
      } }),
      el("button", { class: "btn", type: "button", text: "Use my phone's calendars", onclick: async () => {
        if (await act(() => post("/api/calendar/sources", { id: "phone", kind: "phone", name: "Phone" }), "Added")) render();
      } })))));

  // phone
  const pairBox = el("div", {});
  const deviceRows = devices.devices.map((d) => el("div", { class: "field-row" },
    el("div", {}, el("div", { class: "title", text: d.name }),
      el("div", { class: "meta", text: `${d.model || ""} ${d.os || ""} · ${d.connected ? "connected" : `last seen ${ago(d.last_seen)}`}${d.state?.battery !== undefined ? ` · battery ${d.state.battery}%` : ""}` })),
    el("button", { class: "btn small danger", type: "button", text: "Unpair", onclick: async () => {
      if (await act(() => post(`/api/devices/${encodeURIComponent(d.id)}/revoke`), "Unpaired")) render();
    } })));
  left.append(card("Android phone", "", el("div", { class: "body" }, deviceRows.length ? deviceRows : empty("No phone is paired."),
    el("div", { class: "tags" }, el("button", { class: "btn primary", type: "button", text: "Pair a phone", onclick: async () => {
      const p = await act(() => post("/api/pair/new"));
      if (!p) return;
      pairBox.replaceChildren(el("div", { class: "qr" },
        p.qr_data_uri ? el("img", { src: p.qr_data_uri, alt: "Pairing QR code" }) : null,
        el("div", {}, el("p", { text: "Scan with the phone's camera, or type this into the Felix app:" }),
          el("p", { class: "code", text: p.code }),
          el("p", { class: "mono", text: `${p.hosts.join(", ")}  port ${p.port}` }),
          el("p", { class: "hint", text: `Valid for ${Math.round(p.expires_in / 60)} minutes, single use. Phone and laptop must be on the same network.` }))));
    } })), pairBox)));

  // whatsapp
  const bridge = status.bridge;
  const qrBox = el("div", {});
  if (bridge.pairing_code) {
    qrBox.replaceChildren(el("div", { class: "qr" }, el("div", {},
      el("p", { class: "code", text: bridge.pairing_code }),
      el("p", { class: "hint", text: "On your phone: WhatsApp > Linked devices > Link a device > Link with phone number instead, then type this code." }))));
  } else if (bridge.state === "qr") {
    api("/api/whatsapp/qr").then((q) => q.data_uri && qrBox.replaceChildren(el("div", { class: "qr" },
      el("img", { src: q.data_uri, alt: "WhatsApp link QR code" }),
      el("div", {},
        el("p", { class: "hint", text: "On your phone: WhatsApp > Linked devices > Link a device, and point the camera here. It will be listed as Chrome." }),
        el("button", { class: "btn small", type: "button", text: "Link with a code instead", onclick: async () => {
          if (await act(() => post("/api/whatsapp/bridge", { action: "pairing_code" }))) { await refreshStatus(); render(); }
        } }))))).catch(() => {});
  }
  const numberId = el("input", { type: "text", value: c.whatsapp.cloud.phone_number_id }), template = el("input", { type: "text", value: c.whatsapp.cloud.template });
  const cloudOn = el("input", { type: "checkbox", checked: c.whatsapp.cloud.enabled });
  const cloudToken = el("input", { type: "password", placeholder: settings.secrets.whatsapp_cloud_token ? "A token is saved" : "Access token", autocomplete: "off" });
  right.append(card("WhatsApp", "", el("div", { class: "body" },
    el("div", { class: "field-row" }, el("div", {}, el("div", { class: "title", text: "Through the phone app" }),
      el("div", { class: "meta", text: "Reads notifications, replies through them. Needs a paired phone." })),
      el("span", { class: `tag ${status.whatsapp.phone.ready ? "good" : ""}`, text: status.whatsapp.phone.ready ? "ready" : "phone not connected" })),
    el("div", { class: "field-row" }, el("div", {}, el("div", { class: "title", text: "Linked device (unofficial)" }),
      el("div", { class: "meta", text: bridge.error || "Reads and sends everything, including briefs to your own chat. Not permitted by WhatsApp's terms - see docs/WHATSAPP.md." })),
      el("div", { class: "row-actions" }, el("span", { class: `tag ${bridge.state === "open" ? "good" : bridge.state === "error" ? "bad" : ""}`, text: bridge.state || "off" }),
        el("button", { class: "btn small", type: "button", text: bridge.enabled ? "Turn off" : "Turn on", onclick: async () => {
          await act(() => post("/api/whatsapp/bridge", { action: bridge.enabled ? "stop" : "start" })); await refreshStatus(); render();
        } }))),
    qrBox,
    el("div", { class: "field-row" }, el("label", {}, cloudOn, " Official Business Cloud API (send-only, for briefs)"), el("span", {})),
    el("div", { class: "form-grid" }, field("Phone number ID", numberId), field("Fallback template name", template), el("div", { class: "wide" }, field("Access token", cloudToken))),
    el("div", { class: "tags" }, el("button", { class: "btn primary", type: "button", text: "Save", onclick: async () => {
      if (cloudToken.value.trim()) await post("/api/secrets", { name: "whatsapp_cloud_token", value: cloudToken.value });
      if (await act(() => post("/api/settings", { whatsapp: { cloud: { enabled: cloudOn.checked, phone_number_id: numberId.value.trim(), template: template.value.trim() } } }), "Saved")) render();
    } })))));

  const log = card("Activity log", "Every action Felix took or asked to take", el("div", { class: "body flush" },
    el("table", { class: "log" }, el("thead", {}, el("tr", {}, ["When", "Who", "What", "Result"].map((h) => el("th", { text: h })))),
      el("tbody", {}, audit.entries.map((e) => el("tr", {}, el("td", { text: ago(e.ts) }), el("td", { text: e.actor }),
        el("td", { text: e.action }), el("td", { text: e.outcome })))))));
  return el("div", { class: "col" }, el("div", { class: "cols even" }, left, right), log);
};

// ---------------------------------------------------------------- chat column
const SUGGESTIONS = ["Brief me", "What's broken?", "Who is waiting on me?", "What's on tomorrow?", "Unread emails", "Find 45 minutes this week"];
function bubble(role, text) {
  const node = el("div", { class: `msg ${role}` });
  if (role === "felix") felixRender(node, text || ""); else node.textContent = text || "";
  $("chat-log").append(node);
  $("chat-log").scrollTop = $("chat-log").scrollHeight;
  return node;
}
function miniApprovals(node, approvals) {
  if (!approvals?.length) return;
  const row = el("div", { class: "mini" });
  for (const approval of approvals) {
    row.append(
      el("button", { class: "btn small primary", type: "button", text: `Approve #${approval.id}`, onclick: async () => {
        const result = await act(() => post(`/api/approvals/${approval.id}/approve`), (r) => r.message);
        if (result) { row.remove(); refreshStatus(); render(); }
      } }),
      el("button", { class: "btn small", type: "button", text: "Review", onclick: () => navigate("approvals") }),
      el("button", { class: "btn small danger", type: "button", text: "Cancel", onclick: async () => {
        if (await act(() => post(`/api/approvals/${approval.id}/deny`), "Cancelled")) { row.remove(); refreshStatus(); render(); }
      } }));
  }
  node.append(row);
}
async function ask(text) {
  text = (text || "").trim();
  if (!text || state.busy) return;
  state.busy = true;
  $("chat").classList.add("open");
  $("chat-log").querySelector(".welcome")?.remove();
  bubble("user", text);
  const reply = bubble("felix", "");
  reply.classList.add("busy");
  let written = "";
  try {
    const response = await fetch("/api/chat/stream", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, thread: "main" }) });
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
        if (name === "delta") { written += data.text; felixRender(reply, written); }
        else if (name === "tool" && data.status === "start" && !written) reply.textContent = `Checking ${data.name.replace(/_/g, " ")}`;
        else if (name === "done") { felixRender(reply, data.reply); miniApprovals(reply, data.approvals); }
        else if (name === "error") reply.textContent = data.message;
        $("chat-log").scrollTop = $("chat-log").scrollHeight;
      }
    }
  } catch (error) {
    reply.textContent = error.message;
  } finally {
    reply.classList.remove("busy");
    state.busy = false;
    refreshStatus();
    render();
  }
}
async function loadTranscript() {
  const data = await api("/api/transcript?thread=main&limit=30");
  const log = $("chat-log");
  log.replaceChildren();
  if (!data.turns.length) log.append(el("p", { class: "welcome", text: "Ask about your day, your inbox or your calendar, or give a command. Anything Felix wants to send in your name waits for your approval." }));
  for (const turn of data.turns) bubble(turn.role === "user" ? "user" : "felix", turn.text);
}
function setupChat() {
  $("chips").replaceChildren(...SUGGESTIONS.map((s) => el("button", { type: "button", text: s, onclick: () => ask(s) })));
  const input = $("chat-input");
  const grow = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 140)}px`; };
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
  $("chat-new").addEventListener("click", async () => { await post("/api/chat/reset", { thread: "main" }); $("chat-log").replaceChildren(); toast("Started a fresh conversation"); });
  $("chat-pop").addEventListener("click", () => window.open("/overlay", "felix-overlay", "popup,width=420,height=600"));
  $("chat-toggle").addEventListener("click", () => { $("chat").classList.toggle("open"); input.focus(); });
  $("chat-close").addEventListener("click", () => $("chat").classList.remove("open"));
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("chat").classList.add("open"); input.focus(); }
  });
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (Recognition) {
    const mic = $("mic");
    mic.hidden = false;
    mic.addEventListener("click", () => {
      const recognition = new Recognition();
      recognition.lang = navigator.language || "en-US";
      recognition.interimResults = false;
      mic.classList.add("live");
      recognition.onresult = (event) => { input.value = event.results[0][0].transcript; grow(); };
      recognition.onend = () => mic.classList.remove("live");
      recognition.onerror = () => { mic.classList.remove("live"); toast("Could not hear that"); };
      recognition.start();
    });
  }
}

// ---------------------------------------------------------------- start
function live() {
  const source = new EventSource("/api/events");
  let timer = 0;
  source.addEventListener("update", (event) => {
    const topic = JSON.parse(event.data).topic;
    if (topic === "pulse.flagged") toast("Felix flagged something as broken or urgent");
    if (topic === "nudge") toast(JSON.parse(event.data).data.title);
    clearTimeout(timer);
    timer = setTimeout(() => { refreshStatus(); if (!state.busy && !document.activeElement?.matches("input, textarea, select")) render(); }, 700);
  });
}
function start() {
  $("net-pill").addEventListener("click", () => { $("mode-menu").hidden = !$("mode-menu").hidden; });
  for (const button of $("mode-menu").querySelectorAll("button")) {
    button.addEventListener("click", async () => {
      $("mode-menu").hidden = true;
      if (await act(() => post("/api/mode", { mode: button.dataset.mode }), `Mode: ${button.dataset.mode}`)) { await refreshStatus(); render(); }
    });
  }
  $("sync-btn").addEventListener("click", async () => {
    const result = await act(() => post("/api/sync", { what: "all" }));
    if (result) toast(`Mail: ${result.mail.errors[0] || `${result.mail.new} new`} · Calendar: ${result.calendar.errors[0] || "up to date"}`);
    await refreshStatus(); render();
  });
  window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
  setupChat();
  navigate(location.hash.slice(1) || "today");
  refreshStatus();
  loadTranscript().catch(() => {});
  setInterval(refreshStatus, 30000);
  live();
}
start();
