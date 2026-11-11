# Felix phone link protocol (v1)

How the Android companion talks to Felix Core. The phone always initiates the
connection; Core never needs to reach the phone directly, so it works behind
NAT and survives the phone changing networks.

## Transport and trust

* HTTPS to Core's phone listener (default port `8766`). Core uses a self-signed
  certificate that it generates on first use.
* The phone **pins** that certificate: it stores the SHA-256 of the certificate's
  DER encoding (lower-case hex, no separators) at pairing time and refuses any
  other certificate. Host names are not verified, because the pin already
  identifies Core and its address may change.
* `http` is accepted only when the pairing link says `s=http` (local testing).
* Every request after pairing carries `Authorization: Bearer <token>`.
* All bodies are UTF-8 JSON. Timestamps named `*_ms` / `ts` are Unix epoch
  **milliseconds**.

## Pairing

Core shows a QR code that encodes a deep link:

```
felix://pair?v=1&s=https&h=192.168.1.20,felix-laptop.local&p=8766&c=482913&fp=<sha256hex>&n=VK%27s%20laptop
```

| param | meaning |
|-------|---------|
| `v`   | protocol version (`1`) |
| `s`   | scheme: `https` (default) or `http` |
| `h`   | comma-separated host candidates, tried in order; the phone remembers the one that works and falls back to the others |
| `p`   | port |
| `c`   | one-time pairing code, valid 10 minutes, single use |
| `fp`  | certificate fingerprint to pin (absent for `http`) |
| `n`   | display name of this Core |

`POST /api/phone/pair` (no auth)

```json
{"code": "482913", "device_id": "<uuid the app generated once>", "name": "Pixel 8",
 "model": "Google Pixel 8", "os": "Android 15", "app_version": "0.1.0"}
```

`200` → `{"token": "<opaque>", "device_id": "...", "core": {"name": "...", "version": "0.1.0"}, "poll_wait": 25}`

`403 {"error": "invalid_code"}` for a wrong, used or expired code. `429` when
too many attempts were made.

Any later `401 {"error": "unauthorized"}` means the device was revoked: the app
must stop syncing and ask the user to pair again.

## Phone → Core: events

`POST /api/phone/events`

```json
{"events": [{"id": "<uuid>", "type": "message", "ts": 1794000000000, "data": {}}]}
```

→ `{"accepted": ["<uuid>", "..."]}`

Delivery is at-least-once: the phone keeps events in a persistent queue and
removes one only after Core lists its id in `accepted`. Core ignores ids it has
already seen. At most 100 events per request.

| type | data |
|------|------|
| `message` | `channel` (`whatsapp`, `whatsapp_business`, `sms`, `telegram`, `teams`, `slack`, `signal`, `other`), `package`, `chat_key` (stable per conversation), `chat_name`, `is_group`, `sender`, `text`, `msg_ts`, `can_reply`, `notification_key` |
| `notification` | `package`, `app`, `key`, `title`, `text`, `big_text`, `category`, `post_ts`, `ongoing` |
| `notification_removed` | `key` |
| `sms` | `from`, `text`, `msg_ts` |
| `call` | `state` (`ringing`, `missed`, `answered`, `ended`), `number`, `name` |
| `status` | `battery` (0-100), `charging`, `network` (`wifi`, `cellular`, `none`), `ringer` (`normal`, `vibrate`, `silent`), `dnd`, `screen_on`, `locked`, `permissions` (object of booleans: `notifications`, `accessibility`, `overlay`, `sms`, `contacts`, `calendar`, `call`, `location`) |

`chat_key` for messaging apps is `"<package>|<conversation id>"`, where the
conversation id is the notification's shortcut id when present, else its tag,
else the conversation title.

## Core → phone: commands

`GET /api/phone/poll?wait=25` is a long poll. It answers as soon as a command
is queued, or with an empty list after `wait` seconds. The phone polls again
immediately.

```json
{"commands": [{"id": 123, "type": "wa.reply", "args": {"chat_key": "...", "text": "On my way"}, "expires": 1794000060000}],
 "server_ts": 1794000000000}
```

Commands may be delivered more than once; the phone remembers recent ids and
answers a repeat with the stored result instead of acting twice. A command
whose `expires` has passed is answered with `error: "expired"` and not run.

`POST /api/phone/results`

```json
{"results": [{"id": 123, "ok": true, "data": {}},
             {"id": 124, "ok": false, "error": "no_reply_action", "message": "No open notification for that chat"}]}
```

→ `{"ok": true}`

### Command catalogue

| type | args | result data / errors |
|------|------|----------------------|
| `ping` | – | `{"pong": true}` |
| `status` | – | same object as the `status` event |
| `notify` | `title`, `text`, `big_text?`, `tag?` | shows a Felix notification (used for briefs) |
| `wa.reply` | `chat_key`, `text` | replies through the notification's inline-reply action. Error `no_reply_action` |
| `wa.send` | `number` (E.164), `text`, `package?` | opens the chat and presses Send via accessibility. Errors `accessibility_off`, `screen_locked`, `timeout`, `not_installed` |
| `sms.send` | `to`, `text` | error `permission_denied` |
| `call.dial` | `number` | places the call if permitted, otherwise opens the dialler (`{"placed": false}`) |
| `app.open` | `name?`, `package?` | `{"package": "..."}`; error `not_found` |
| `url.open` | `url` | |
| `alarm.set` | `hour`, `minute`, `label?` | |
| `timer.set` | `seconds`, `label?` | |
| `torch` | `on` | |
| `volume` | `stream` (`media`, `ring`, `alarm`, `notification`), `level` (0-100) | |
| `ringer` | `mode` (`normal`, `vibrate`, `silent`) | error `permission_denied` when DND access is missing |
| `dnd` | `on` | error `permission_denied` |
| `brightness` | `level` (0-100) | error `permission_denied` |
| `locate` | `seconds?` (default 30) | rings at full volume even when silenced |
| `location` | – | `{"lat", "lon", "accuracy", "ts"}`; error `permission_denied`, `unavailable` |
| `contacts.search` | `query`, `limit?` | `{"contacts": [{"name", "phones": [], "emails": []}]}` |
| `contacts.dump` | `limit?` | same shape, every contact |
| `calendar.calendars` | – | `{"calendars": [{"id", "name", "account", "primary", "writable"}]}` |
| `calendar.list` | `start_ms`, `end_ms` | `{"events": [{"id", "instance_start_ms", "title", "start_ms", "end_ms", "all_day", "location", "description", "calendar", "calendar_id", "organizer", "attendees": [{"name", "email", "status"}], "tz"}]}` |
| `calendar.create` | `title`, `start_ms`, `end_ms`, `all_day?`, `location?`, `description?`, `calendar_id?`, `tz?`, `attendees?` (emails), `reminders?` (minutes) | `{"id": "..."}` |
| `calendar.update` | `id` + any create field | |
| `calendar.delete` | `id` | |
| `ui.global` | `action` (`back`, `home`, `recents`, `notifications`, `quick_settings`, `lock`, `screenshot`) | error `accessibility_off` |
| `ui.click` | one of `text`, `id`, `desc`; `index?` | error `not_found` |
| `ui.type` | `text`, `submit?` | types into the focused field |
| `ui.scroll` | `direction` (`up`, `down`, `left`, `right`) | |
| `ui.swipe` | `x1`, `y1`, `x2`, `y2`, `ms?` | |
| `ui.dump` | – | `{"package": "...", "nodes": [{"text", "desc", "id", "cls", "clickable", "bounds": [l, t, r, b]}]}` |
| `clipboard.set` | `text` | |
| `clipboard.get` | – | `{"text": "..."}` (Android only allows this while Felix is in the foreground) |
| `tts` | `text` | speaks aloud |
| `media` | `action` (`play_pause`, `next`, `previous`, `stop`) | |
| `notifications.list` | – | `{"notifications": [...]}` (same fields as the `notification` event) |
| `notification.dismiss` | `key` | |
| `sms.list` | `limit?`, `since_ms?` | `{"messages": [{"from", "text", "ts", "read"}]}` |
| `calls.list` | `limit?` | `{"calls": [{"number", "name", "type", "ts", "duration"}]}` |

Unknown command types are answered with `error: "unsupported"` so Core can tell
an old app from a failure.

## Assistant endpoints used by the overlay

`GET /api/phone/hello` → `{"core": {"name", "version"}, "online": true, "mode": "auto", "time": 1794000000000}`

`POST /api/chat` with `{"text": "...", "thread": "phone"}` →
`{"reply": "...", "engine": "claude|local|rules", "online": true, "approvals": [{"id", "tool", "preview", "risk"}]}`

`POST /api/chat/stream` (same body) answers `text/event-stream`:

```
event: delta      data: {"text": "partial text"}
event: tool       data: {"name": "calendar_list", "status": "start"}
event: approval   data: {"id": 7, "tool": "whatsapp_send", "preview": "...", "risk": "outbound"}
event: done       data: {"reply": "full text", "engine": "claude", "approvals": [...]}
event: error      data: {"message": "..."}
```

`GET /api/approvals?status=pending` → `{"approvals": [{"id", "tool", "preview", "risk", "created_at"}]}`

`POST /api/approvals/<id>/approve` → `{"ok": true, "message": "..."}`

`POST /api/approvals/<id>/deny` → `{"ok": true}`

`GET /api/brief/latest` → `{"kind": "morning", "day": "2026-11-12", "text": "...", "created_at": 1794000000000}`

## When Core is unreachable

The phone keeps capturing events into its queue (bounded; oldest plain
notifications are dropped first, messages last), runs device commands typed
into the overlay through its own offline command parser, and retries the link
with exponential backoff capped at one minute.
