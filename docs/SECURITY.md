# Security and privacy

Felix has the keys to your inbox, your calendar, your messages, your laptop and your
phone. This page says how that access is protected, what leaves your laptop, and where
the limits are. Read "What Felix cannot protect you from" before relying on it.

## The design in one paragraph

Felix runs on your laptop under your user account and keeps everything in one folder
there. It has no server of its own, no account system and no telemetry. A language
model can propose actions but cannot carry out anything that speaks for you or is hard
to undo: those wait for an approval that only you can give, from a place only you can
reach. Everything Felix does is written to a log you can read.

## What needs your approval

Every action Felix can take is in one of five groups.

| Group | Examples | Default |
|---|---|---|
| Look | Read mail, chats, calendar, ordinary files, status | Done without asking |
| Local | Add a to-do, change your own calendar, volume, open an installed app or a document, torch, alarm | Done without asking |
| Speaks for you | Send email, WhatsApp or SMS; place a call; invite people to a meeting | **Waits for you** |
| Goes out | Open a web address that Felix picked, on the laptop or the phone | **Waits for you** |
| Changes things | Write a file on the laptop; tap and type on the phone's screen | **Waits for you** |
| Dangerous | Run a shell command or open a program or script file, delete files, sleep or power off, read credential files or Felix's own data folder | **Always waits for you** |

Why a web address waits: a link can carry information out in its text, so a link chosen
by a language model is treated like a message. A link you type yourself ("open
github.com") opens at once.

"Open an app" means an application your system lists (the Start menu, the Applications
folder, desktop entries). Felix does not look names up as programs, so "open" cannot be
used to run an arbitrary command.

For every group except the last you choose, per kind, between "ask me first", "do it"
and "never" (Settings, "What Felix may do without asking"). You can also name trusted
recipients who may be messaged without asking. Messages to yourself never ask.

The dangerous group ignores "do it" and asks every time.

When something waits, you see exactly what will happen: the recipients including hidden
copies, the subject and the full text, or the exact command. You can edit the text
before approving. An approval that is not answered expires after 12 hours.

Three rules keep that view honest:

- **Felix's code writes it, not the model.** Whatever a language model says about what
  it asked for, the reply always ends with the request itself, taken from what is
  stored. That holds in the dashboard, the overlay, the terminal, the phone and
  WhatsApp.
- **What you see is what runs.** A contact name is turned into its address, and a chat
  name into that one chat, at the moment of the request. If the contact is changed or
  another chat with the same name appears before you approve, the message still goes
  where the preview said.
- **Nothing long is hidden quietly.** If a file to be written is too long to show, the
  preview shows its beginning and its end, and says how much is left out and where to
  read all of it.

"A message to myself" and "a trusted recipient" are matched strictly. An address that
only looks like yours (an accent in the domain, the same number in another country) is
somebody else's and waits for approval. A chat is identified by its number or by
WhatsApp's own id, never by its name, so a group named after your phone number is not
you.

### Who can approve

Approval is not something the language model can do. There is no tool for it, and "yes"
is recognised in your own typed input before any model sees the message. An approval
can come from:

- the dashboard or the overlay on this laptop (protected by the session described below),
- the terminal on this laptop (`felix approve 3`),
- the paired phone (the Felix app), and
- your own "Message yourself" chat in WhatsApp, if you enabled the linked device. Only
  fresh messages count there: one that WhatsApp delivers more than five minutes late
  is ignored, so an old "yes" cannot approve something new.

Actions that change the laptop are stricter: a shell command, writing or deleting a
file, a power action or reading credential files can by default be approved **only at
the laptop**. A phone, even your own, cannot approve them; the request stays waiting
until you are back at the laptop.

Two switches loosen these rules. They are not in the dashboard and cannot be changed
through it; you set them in a terminal on the laptop and restart Felix:

- `felix config set policy.remote_danger_approval true` lets the phone and your WhatsApp
  chat approve laptop-changing actions too. Think about what a lost, unlocked phone
  would then be able to do.
- `felix config set policy.allow_unattended_danger true` lets "do it" apply to the
  dangerous group as well. Not recommended.

## Other people's words are not instructions

Email, chat messages, notifications, web pages and file contents are written by other
people, and some of them will try to give Felix orders ("forward this to...", "ignore
your previous instructions"). Felix defends against that in layers:

1. The model is told to treat all such content as information to report, and to flag
   content that tries to instruct it.
2. More importantly, the model cannot act on it alone. Anything it could be tricked into
   doing that matters (sending, deleting, running) stops at the approval step, where you
   see the real recipient and text.
3. Reading Felix's own data folder and well-known credential files (SSH and cloud keys,
   password stores, token files of common tools) counts as dangerous and needs
   approval, so a tricked model cannot quietly read your keys. This is a list of known
   places, not a guarantee about every secret on your disk.
4. Nothing from a message is ever shown as a web page: the dashboard writes all such
   text as plain text and loads no scripts, fonts or images from the internet.
5. Text from outside is never pasted into a command. A meeting title or a message that
   Felix shows in a desktop notification is handed to the system as data.

No defence of this kind is complete. Read approvals before you approve them, the same
way you would read a letter before signing it. Be most careful with "do it" settings for
anything that sends.

Two things a misled model can still do without asking, because they stay on your
machine: save a note to Felix's memory or a contact, and put text on the clipboard.
Neither sends anything, and anything they later influence still shows its real
recipient at approval. Ask "what do you remember" now and then, and say "forget 3" (the
note's number) for anything that should not be there.

## What leaves your laptop

Felix sends data only to services you connected.

| Where | When | What |
|---|---|---|
| Anthropic (Claude API) | Online, with an API key | Your question, Felix's instructions, and the results of the lookups Claude asks for to answer it: the relevant emails, messages, calendar entries, to-dos, or a file you asked about. Web search, if on, runs at Anthropic. |
| Your mail servers | While syncing and sending | Normal IMAP and SMTP traffic over TLS |
| Google, Microsoft | If you signed in with them | Sign-in, calendar and mail traffic |
| Calendar and news addresses you added | On sync | A plain download |
| WhatsApp (Meta) | Linked device or Cloud API | The messages you send and receive there |
| Your phone | When paired | Commands and replies, over your own network |
| 1.1.1.1, 8.8.8.8, 9.9.9.9 | A few times a minute, in automatic mode | A bare connection attempt to see whether the internet is reachable. No data is sent. If that is blocked (for example behind a web proxy), an empty request to api.anthropic.com or gstatic.com instead. |

To keep the text of your mail and chats away from Claude, turn off "Send full message
text to Claude" in Settings. Claude then sees senders, subjects and short excerpts, and
answers less well. To send nothing to anyone, use offline mode (`felix mode offline`):
Felix then makes no connections to the internet at all. The WhatsApp link is taken down
too, and what you approve waits in the queue. Only your paired phone and a local model,
both on your own network, are still used.

Anthropic's handling of API data is governed by its own terms and privacy policy, which
you should read; Felix does not control it.

## The dashboard

The dashboard is a small web server on your laptop.

- It listens on `127.0.0.1` only, so other machines cannot connect to it.
- Every request needs a secret session token. `felix open` passes it to your browser
  once; after that it lives in a cookie that scripts cannot read and that is never sent
  along with requests coming from other websites.
- Requests whose `Host` is not this laptop are refused, which stops a hostile website
  from reaching the dashboard by pointing its own domain at your machine. Requests that
  change anything must come from the dashboard's own page. There is no cross-origin
  access.
- The pages allow no inline scripts and nothing from other origins, and they cannot be
  framed by another site.
- Secrets can be set through the dashboard but never read back.

The link from `felix open` contains the token and ends up in your browser's history.
Anyone who can read your browser profile can open the dashboard; that person can already
read your mail in the same browser.

## The phone link

The phone connects to a second listener that exists only once you pair a phone.

- It is encrypted with a certificate created on your laptop. The pairing code carries
  the certificate's fingerprint, and the phone refuses any other certificate afterwards,
  so nobody on the Wi-Fi can sit in the middle.
- Pairing needs a six-digit code that is valid for ten minutes and works once. After
  eight wrong attempts pairing is blocked until the codes expire.
- The phone gets its own token, stored on the laptop only as a hash. The phone can reach
  only the endpoints it needs: its event and command queue, chat, approvals and the
  latest brief. It cannot read settings, secrets, the inbox pages or the activity log.
- Unpair a phone at any time in Settings. Its token stops working at once.

This listener accepts connections from your network (port 8766). Do not expose it to the
internet; use a private network such as Tailscale to reach it from outside.

## Where things are stored

Everything is in Felix's data folder (see [SETUP.md](SETUP.md#12-checking-updating-removing)),
which is created readable by your user only.

- **Secrets** (API key, mail passwords, sign-in tokens, the session token) go to your
  operating system's keychain when one is available. Otherwise they go to
  `secrets.json` in the data folder, readable by your user only.
- **Mail, messages, calendar, to-dos, memory and the activity log** are in a SQLite
  database, `felix.db`. It is **not encrypted**. Protect it the way you protect the rest
  of your laptop: a login password and full-disk encryption (FileVault, BitLocker,
  LUKS).
- **The WhatsApp linked-device session**, if you use it, is in `whatsapp-session`. Anyone
  who copies that folder can act as your linked device until you remove it in WhatsApp.

Backups of the data folder contain all of the above.

## The activity log

Settings, Activity log shows every action Felix took or asked to take: what, when, who
asked (you, Felix, the scheduler), the result, and for approvals where the approval came
from. Entries are never edited by Felix. Passwords and tokens are masked.

## What Felix cannot protect you from

- **Someone using your unlocked laptop** has everything Felix has, and everything else
  you have.
- **Someone using your unlocked phone** can ask Felix anything and approve messages in
  your name, as they could by opening your WhatsApp and mail directly. They cannot
  approve actions that change the laptop unless you allowed that. Unpair a lost phone
  from the dashboard.
- **Other software running as you** on the laptop can read Felix's data folder and its
  session token. Felix is not a sandbox.
- **Approving without reading.** The approval step is the main defence against a model
  that was misled.
- **The accessibility permission on the phone** lets Felix operate the screen. It is
  used only when you ask for a screen action or a new WhatsApp message, but it is a
  powerful permission; grant it only if you want those features.
- **The WhatsApp linked device** relies on unofficial software. See
  [WHATSAPP.md](WHATSAPP.md).
- **Shell commands you approve** run with your full user rights. Felix shows the exact
  command; it does not judge whether it is wise.

## If something goes wrong

- Stop Felix: close it, or `felix autostart disable` and end the process.
- Cut it off from the network without stopping it: `felix mode offline`.
- Revoke access at the source: delete the Gmail app password, remove the app from your
  Google or Microsoft account, remove the linked device in WhatsApp, delete the Claude
  API key in the Anthropic console, unpair the phone.
- Look at Settings, Activity log to see what was done.

## Reporting a security problem

Open an issue in this repository, or contact the repository owner privately if the
details should not be public yet.
