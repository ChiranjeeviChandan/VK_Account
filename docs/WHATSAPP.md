# Felix and WhatsApp

WhatsApp has no official way for a personal assistant to read and answer your own
chats. Felix therefore offers three routes. Each has a real limitation, so read this
page before choosing. They can be combined; Felix uses the first one that works, in the
order set by `whatsapp.transports` (linked device, then phone, then Cloud API).

| | Through the phone app | Linked device | Business Cloud API |
|---|---|---|---|
| Official | Uses Android's own notification and accessibility features | **No**: an unofficial client | Yes |
| Read incoming messages | Yes, from notifications (text, sender, chat) | Yes, every chat, as they arrive | No |
| Reply in a chat | Yes, while that chat's notification is open | Yes | No |
| Start a new message | Yes, with the phone unlocked | Yes | Only to your own number |
| Brief to yourself at 07:30 | Only if the phone is unlocked then | Yes | Yes |
| Works with the phone in your pocket, locked | Reading and replying: yes. New messages: no | Yes | Yes |
| Needs | The Felix Companion app, paired | Node.js on the laptop | A Meta business account and a second number |
| Risk to your account | Low (see below) | **WhatsApp may restrict or ban it** | None |

## 1. Through the phone app (start here)

Pair the Android app ([SETUP.md](SETUP.md#6-the-android-phone)) and allow notification
access. From then on:

- Every WhatsApp notification is passed to Felix on your laptop: the chat, who wrote,
  and the text. Felix files it, judges its tone and urgency, and includes it in "waiting
  on you" and the pulse.
- When you approve a reply, the phone sends it through the notification's own reply
  button, the same one you would tap. WhatsApp itself sends the message.
- To start a new message (or to reply after the notification has gone), Felix opens the
  chat in WhatsApp on the phone and presses Send through Android's accessibility
  service. That needs the phone unlocked with the screen on, and you will see WhatsApp
  open briefly.

Limits you will notice:

- Felix sees what notifications show. It does not see message history, pictures, voice
  notes or chats you have muted. Long messages may be shortened by Android.
- If you read a message on the phone and dismiss the notification, Felix can no longer
  reply through it until that chat's next message. It tells you so instead of failing
  silently.
- A brief due while the phone is locked cannot go out by WhatsApp this way. Felix shows
  it as a notification from the Felix app instead, or emails it.

The same route covers SMS and, if you allow them in the app, Telegram, Signal, Teams and
Slack notifications.

About risk: replies go out through Android's standard reply button, which is exactly
what it is for. Starting a new message has Felix press WhatsApp's own Send button for
you, one approved message at a time. That is not bulk or unattended messaging, and no
action against it is known, but WhatsApp's terms are written broadly about automation,
so it cannot be called risk-free.

## 2. Linked device (unofficial, opt in)

Felix can link to your account the way WhatsApp Web and WhatsApp Desktop do. It then
receives every message as it arrives and can send to any chat, with the phone locked or
even switched off for a while. This is the only route where:

- your morning and evening briefs always arrive in your own WhatsApp, and
- you can **type to Felix inside WhatsApp**: open your own "Message yourself" chat and
  write "what's broken" or "reply to Anita saying we will fix it today". Felix answers
  in that chat, each answer starting with "Felix:". Approvals work there too: reply
  "yes" or "no".

### The risk, plainly

This route uses [Baileys](https://github.com/WhiskeySockets/Baileys), an open-source
library that speaks WhatsApp's protocol without WhatsApp's permission. WhatsApp's terms
of service do not allow unofficial clients. WhatsApp can and does restrict or
permanently ban numbers it believes are automated, and it does not have to warn you
first. The library can also stop working whenever WhatsApp changes its protocol.

Felix keeps its behaviour as close to a person's as it can: it sends only messages you
approved, one at a time, never in bulk, and it does not mark you as online. That lowers
the risk. It does not remove it. If losing this WhatsApp number would be serious for
you, use the phone route and accept its limits.

### Setting it up

1. Install [Node.js](https://nodejs.org) 20 or newer.
2. Run:

```bash
felix connect whatsapp
```

   Answer yes to the linked-device question. Felix downloads the bridge's packages once
   (into its data folder) and enables it.
3. Start Felix, open Settings, WhatsApp. A QR code appears. On the phone open WhatsApp,
   then Linked devices, then Link a device, and scan it. No camera to hand? Choose
   "Link with a code instead" and type the code on the phone under "Link with phone
   number instead".

Felix appears in WhatsApp's list of linked devices as **Chrome**. To disconnect, press
Unlink in Felix's settings, or remove that device in WhatsApp on the phone. Either way
the saved session is deleted from the laptop.

Good to know:

- WhatsApp disconnects linked devices if the phone has not been online for about two
  weeks.
- Felix receives messages from the moment it is linked. It does not import your chat
  history.
- Turn off commands in "Message yourself" with
  `felix config set whatsapp.bridge.command_chat false`.
- This part of Felix could not be tested against the real WhatsApp service when it was
  written. Its logic is tested with a scripted connection. Expect to report problems.

## 3. WhatsApp Business Cloud API (official, send-only)

Meta's official API can send messages from a business number to your own number. Felix
uses it for one thing: delivering briefs and alerts to you. It cannot read your personal
chats, and Felix refuses to use it for anyone but you.

You need a Meta developer account, a WhatsApp Business app, and a phone number that is
not already registered in the WhatsApp app (Meta offers a test number).

1. At <https://developers.facebook.com> create an app of type Business and add the
   WhatsApp product.
2. On the app's WhatsApp "API setup" page note the **Phone number ID**, add your own
   number as a recipient, and create a permanent access token (a System User token with
   the `whatsapp_business_messaging` permission; the token shown on the page expires
   after a day).
3. In Felix, open Settings, WhatsApp, tick "Official Business Cloud API", and enter the
   phone number ID and the token.

The 24-hour rule: Meta delivers ordinary text only if you have messaged the business
number within the last 24 hours. Outside that window it accepts only a template you had
approved in advance. To get your 07:30 brief reliably, create a template with one text
variable in the body (for example `Felix brief: {{1}}`), wait for Meta to approve it,
and enter its name in Felix as the fallback template. Felix then sends the brief as one
condensed line through the template whenever the window is closed. If the template's
language code is not `en` (Meta often uses `en_US`), set it with
`felix config set whatsapp.cloud.template_lang en_US`. Meta charges for template
messages; check its current pricing.

## Which should I use?

- **Start with the phone app.** It is safe, and it covers reading and replying.
- **Add the Cloud API** if you want briefs in WhatsApp every morning without any risk to
  your personal account, and do not mind the setup and the condensed format.
- **Add the linked device** if you want Felix to work fully inside WhatsApp and you
  accept that the account could be restricted.

Whatever you choose, the brief always has a fallback: a notification from the Felix app,
then email, then a desktop notice.
