# Setting up Felix

This guide takes you from nothing to a working assistant. Each part is optional: Felix
works with whatever you have connected, and the dashboard's Today page lists what is
still missing.

1. [Install](#1-install)
2. [First run](#2-first-run)
3. [Claude: the online brain](#3-claude-the-online-brain)
4. [Email](#4-email)
5. [Calendars](#5-calendars)
6. [The Android phone](#6-the-android-phone)
7. [WhatsApp](#7-whatsapp)
8. [Morning and evening briefs](#8-morning-and-evening-briefs)
9. [Start Felix when you log in](#9-start-felix-when-you-log-in)
10. [Working without the internet](#10-working-without-the-internet)
11. [Using Felix away from your desk](#11-using-felix-away-from-your-desk)
12. [Checking, updating, removing](#12-checking-updating-removing)

## 1. Install

You need Python 3.10 or newer. Everything else is installed for you.

**macOS and Linux**

```bash
./scripts/install.sh
```

**Windows** (in Command Prompt or PowerShell, from the repository folder)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

Both create a private Python environment and a `felix` command. Nothing is installed
system-wide. To install by hand instead, run `pip install .` inside an environment of
your own.

The installer ends by telling you how to start Felix, and two things are worth knowing:

- **The `felix` command works in terminal windows opened after the install.** The
  window you installed from was opened before, so on Windows it does not know the
  command yet: open a new one, or use the Start menu, where the installer puts "Felix"
  and "Felix demo". On macOS and Linux the installer writes the command out in full if
  its folder (`~/.local/bin`) is not on your PATH, and shows the line that adds it.
- **Use a normal window, not an administrator one, and no `sudo`.** Felix does not need
  administrator rights, and everything it did on the laptop would have them too. It
  warns you if it is started that way.

## 2. First run

```bash
felix demo
```

This opens Felix on sample data so you can see what it does. Nothing in the demo is ever
sent, and it uses a separate data folder. When you are ready:

```bash
felix setup
```

```bash
felix start
```

`felix setup` asks for your first name, email address, WhatsApp number, time zone and the
times of your two briefs. `felix start` runs Felix and opens the dashboard in your
browser. Keep that terminal open, or see
[Start Felix when you log in](#9-start-felix-when-you-log-in). Running `felix start`
when Felix is already running simply opens the dashboard. To stop Felix, press Ctrl+C in
its window, or from any terminal:

```bash
felix stop
```

The dashboard only opens from this laptop, through the link that `felix start` or
`felix open` gives you. If the browser says Felix is locked, run `felix open`.

## 3. Claude: the online brain

With a Claude API key Felix understands anything you ask. Without one it follows direct
commands only (say "help" to see them).

1. Create a key at <https://console.anthropic.com> (Settings, then API keys). Usage is
   billed to your Anthropic account.
2. Add it:

```bash
felix connect claude
```

You can also paste it in the dashboard under Settings, Brain. The key is stored in your
operating system's keychain when one is available, otherwise in a file only you can
read. It is never shown again.

Settings you may want to change (dashboard, Settings, Brain):

| Setting | Default | What it does |
|---|---|---|
| Model | `claude-opus-5-5` | Any Claude model your key can use. |
| Effort | low | Low answers fastest. Raise it for harder questions at the cost of speed. |
| Fast mode | off | Quicker replies at a higher price, on models that offer it. |
| Web search | on | Lets Claude look up current information. |
| Send full message text to Claude | on | Off: only short excerpts of your mail and chats leave the laptop. |

One more default worth knowing about: **refusal fallback is on**. If a request is
declined by a safety check on the model you chose, the Claude API answers it with the
model Anthropic recommends for that case instead of returning nothing. The dashboard and
the overlay note under the reply when a different model answered. Turn it off with:

```bash
felix config set brain.refusal_fallback false
```

(`felix config set` changes the saved settings; restart Felix if it is running.)

## 4. Email

Felix reads mail over IMAP and sends over SMTP, the same way a desktop mail program does.

```bash
felix connect mail
```

Gmail, Outlook.com, Microsoft 365, Yahoo, iCloud, Zoho and Fastmail are recognised from
the address. For anything else you will be asked for the incoming and outgoing servers.

### Gmail with an app password (simplest)

1. Turn on 2-Step Verification for your Google account if it is not on already.
2. Open <https://myaccount.google.com/apppasswords>, create an app password called
   "Felix" and copy the 16 characters.
3. Run `felix connect mail`, enter your Gmail address and paste the app password.

An app password gives access to mail only and can be revoked from the same page at any
time. Some Google Workspace organisations disable app passwords; use sign-in with Google
below in that case.

### Sign in with Google (Gmail and Google Calendar)

This uses an OAuth client that belongs to you, so the access tokens are issued to you
and to nobody else.

1. Open the [Google Cloud console](https://console.cloud.google.com/), create a project
   (any name).
2. In "APIs and services", then "Library", enable the **Google Calendar API**.
3. In "OAuth consent screen" choose **External** (or **Internal** if you use Google
   Workspace and only want your own organisation), fill in the app name and your email,
   and add yourself under "Test users".
4. In "Credentials" choose "Create credentials", "OAuth client ID", application type
   **Desktop app**. Copy the client ID and client secret.
5. Run:

```bash
felix connect google
```

Paste the client ID and secret. A browser window opens for Google's consent screen;
Felix receives the result on a temporary port on this laptop only.

Two things to know. Google shows a warning screen for apps it has not verified; choose
"Advanced" and continue, since this is your own app. And while an External app is in
"Testing", Google expires its sign-in after 7 days. To avoid signing in every week,
press "Publish app" on the consent screen page (you do not need to complete Google's
verification for personal use).

### Microsoft 365 and Outlook.com

Microsoft no longer accepts passwords for mail programs, so Felix signs in with a device
code.

1. Open the [Microsoft Entra admin centre](https://entra.microsoft.com/), go to
   "App registrations" and choose "New registration". Name it "Felix". Under "Supported
   account types" pick the option that includes your account. Leave the redirect address
   empty.
2. Open the new registration. Under "Authentication" turn on **Allow public client
   flows**.
3. Under "API permissions" add these delegated permissions: `IMAP.AccessAsUser.All`,
   `SMTP.Send` and `offline_access`. In a company tenant an administrator may have to
   grant consent.
4. Copy the "Application (client) ID" and the "Directory (tenant) ID" from the overview
   page, then run:

```bash
felix connect microsoft
```

Felix prints a short code and an address. Open the address in any browser, enter the
code and approve.

If sending fails with an authentication error, "Authenticated SMTP" is switched off for
your mailbox. A Microsoft 365 administrator can turn it on in the admin centre under
Users, Active users, your user, Mail, Manage email apps.

### What Felix does with mail

- It syncs the last 14 days every 3 minutes while online (both adjustable in the config).
- Newsletters and automated mail are recognised and kept out of "waiting on you".
- Replies and new mail are queued and sent when you approve them. Offline, they wait and
  go out by themselves when the connection returns.
- A copy of what you send is filed in your Sent folder, and the original is marked as
  answered, as it would be from any mail program.

## 5. Calendars

Felix always has a calendar of its own, which works offline. You can add others:

| Kind | How | Read | Write |
|---|---|---|---|
| A calendar address (ICS) | `felix connect calendar` or Settings, Calendars | yes | no |
| Google Calendar | `felix connect google` (see above) | yes | yes |
| The phone's calendars | Pair the phone, then "Use my phone's calendars" in Settings | yes | yes |

Most calendar services can give you a private ICS address: in Google Calendar it is
under Settings, your calendar, "Secret address in iCal format"; in Outlook it is under
Settings, Calendar, Shared calendars, "Publish a calendar". Treat that address like a
password.

The phone's calendars are the easiest way to reach a company Exchange calendar: whatever
the phone syncs, Felix can read and change.

Changing your own calendar does not need approval by default. Anything that invites or
notifies other people does.

## 6. The Android phone

The phone app, Felix Companion, lets Felix read and answer message notifications, place
calls, send texts, set alarms, find the phone, use its calendars and float over other
apps as a bubble.

1. **Get the app.** Download `felix-companion-debug` from the repository's Actions tab
   on GitHub (workflow "Android companion"), or build it yourself. Both are described in
   [android/README.md](../android/README.md).
2. **Pair.** With Felix running on the laptop:

```bash
felix pair
```

   or use Settings, Android phone, "Pair a phone" in the dashboard. Scan the QR code
   with the phone's camera, or type the address, port and six-digit code into the app.
   The code works once and for ten minutes.
3. **Grant permissions** in the app. Each one is optional and says what it is for.
   Notification access is the one that lets Felix see WhatsApp messages.

The phone and the laptop must be able to reach each other: the same Wi-Fi, or a private
network (see [Using Felix away from your desk](#11-using-felix-away-from-your-desk)).
The connection is encrypted, and the phone accepts only your laptop's certificate.

If your laptop has a firewall, allow incoming connections on port 8766 from your home or
office network.

## 7. WhatsApp

There are three ways for Felix to reach WhatsApp, and they can be combined. The short
version:

- **Through the phone app** (recommended first): nothing unofficial. Felix reads
  WhatsApp notifications and replies through them.
- **As a linked device**: Felix can read and send everything, including your briefs to
  your own chat while the phone is locked. It relies on an unofficial client, which
  WhatsApp's terms do not allow.
- **WhatsApp Business Cloud API**: official, send-only, for briefs from a business
  number.

Read [WHATSAPP.md](WHATSAPP.md) before choosing. Then:

```bash
felix connect whatsapp
```

## 8. Morning and evening briefs

Felix sends a brief at 07:30 and a wrap-up at 21:00 in your time zone. Change the times,
turn either off or skip weekends in Settings, "Morning and evening briefs".

The morning brief covers today's meetings and clashes, what is due and overdue, what
looks broken, who is waiting on you, unread counts and good news. The evening one covers
what you finished, what is still open and tomorrow.

Delivery tries each route in order and stops at the first that works:

1. **WhatsApp** to your own number (linked device or Cloud API; through the phone app
   only while the phone is unlocked).
2. **A notification on the phone** from the Felix app.
3. **Email** to yourself.
4. **A desktop notification** on the laptop.

If the laptop is asleep at brief time, the brief is sent when it wakes, up to four hours
late. Ask for one at any time with "brief me", or send it to your WhatsApp from the
Today page.

With a Claude key the brief is rewritten so that the most consequential items come
first and it fits one screen. Without one, or offline, you get the standard layout. The
facts are the same either way: Claude is told to use only what Felix gathered.

## 9. Start Felix when you log in

```bash
felix autostart enable
```

This adds a login item for your user (a systemd user service on Linux, a LaunchAgent on
macOS, a Startup item on Windows). Started this way Felix runs in the background with no
window; `felix open` opens the dashboard and `felix stop` stops it. Undo the login item
with `felix autostart disable`.

Felix has to be running for briefs, reminders and the phone link to work. A laptop that
is asleep cannot send your 07:30 brief; it is sent when the laptop wakes.

## 10. Working without the internet

Felix checks the connection by itself and switches. You can also force it:

```bash
felix mode offline
```

```bash
felix mode auto
```

Offline, Felix still answers from everything already on the laptop, manages its own
calendar and your to-do list, controls the laptop and (over your local network) the
phone, analyses what is broken, and composes briefs. Messages you approve wait in the
queue.

Offline it understands direct commands. For free-form questions without the internet,
install [Ollama](https://ollama.com), pull a model that supports tool use, and Felix
will use it whenever Claude is not reachable:

```bash
ollama pull llama3.1
```

A local model is slower and less capable than Claude. The approval rules are the same.

## 11. Using Felix away from your desk

The phone app talks to your laptop. On the same Wi-Fi that needs nothing more. To reach
the laptop from anywhere, put both on a private network such as
[Tailscale](https://tailscale.com) or WireGuard, then pair again so the pairing code
includes the private address. If Felix does not pick it up by itself, tell it:

```bash
felix config set phone.advertise_hosts '["100.64.0.10"]'
```

(use your laptop's address on the private network, then restart Felix).

Do not forward a port from your router to Felix. It is built to be reached over a
network you trust.

## 12. Checking, updating, removing

**Check the installation**

```bash
felix doctor
```

```bash
felix status
```

**Update.** Stop Felix, pull the new version and run the installer again (on Windows,
the `scripts\install.ps1` command from [Install](#1-install)):

```bash
felix stop
```

```bash
git pull
```

```bash
./scripts/install.sh
```

**Where your data is.** One folder holds everything (database, settings, secrets, logs):

| System | Folder |
|---|---|
| Linux | `~/.local/share/felix` |
| macOS | `~/Library/Application Support/Felix` |
| Windows | `%APPDATA%\Felix` |

Set the `FELIX_HOME` environment variable, or pass `--home`, to use another one.

**Remove Felix.** Run `felix stop` and `felix autostart disable`, delete the data folder
above, delete the installation (`~/.local/share/felix-app` and `~/.local/bin/felix`; on
Windows `%LOCALAPPDATA%\Felix\app` and the "Felix" folder in the Start menu), and unpair
or uninstall the phone app. Revoke
what you granted: the Gmail app password, the Google or Microsoft app sign-in, the
linked device in WhatsApp, and the Claude API key.

## Troubleshooting

| What you see | What to do |
|---|---|
| "'felix' is not recognized" (Windows) or "command not found" | The window was opened before Felix was installed. Open a new terminal window, or start Felix from the Start menu. On macOS and Linux, use the full command the installer printed, or add `~/.local/bin` to your PATH. |
| "Felix is running, so it cannot be updated" | Stop it with `felix stop` (and close the demo's window if it is open), then run the installer again. |
| A warning about administrator rights | Stop Felix and start it from a normal window, without "Run as administrator" or `sudo`. |
| "Felix is locked in this browser" | Open the dashboard with `felix open`. |
| "Felix is not running" | Start it with `felix start`. |
| "I'm working without a language model right now" | No Claude key, or no internet. Add a key, or say "help" to see the commands. |
| Gmail rejects the password | Use an app password, not your normal password. |
| The phone shows "not reachable" | Same network? Laptop firewall allowing port 8766? Battery saver killing the app? See android/README.md. |
| "That chat has no open notification to reply through" | The chat's notification was dismissed on the phone. It works again after that chat's next message, or use the linked device. |
| The brief did not arrive on WhatsApp | Settings, Activity log shows why next to the brief's entry. Usually no WhatsApp route was connected at that time, and it fell back to a phone notification or email. |
| A message is stuck in the queue | Felix retries by itself. `felix status` shows the count; the reason is in Settings, Activity log. |
