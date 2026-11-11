# Felix Companion (Android)

The phone half of Felix. It links your Android phone to Felix Core on your
laptop so Felix can read and answer chat notifications (WhatsApp included),
carry out phone actions, use the calendars synced to the phone, and float over
other apps as a small bubble you can type or talk to.

The app talks to one machine only: the Felix Core you pair it with. There are
no accounts, no analytics and no other servers. It uses Android framework APIs
only (no AndroidX, no third-party libraries), so the APK is small and there is
little to go wrong.

## Get the app

**Easiest: download the build from GitHub.** Every push that touches
`android/` runs the *Android companion* workflow. Open the repository on
GitHub, go to **Actions → Android companion → the latest run**, and download
the artifact **`felix-companion-debug`**. Unzip it and copy the `.apk` to your
phone.

**Or build it yourself.**

* Android Studio: *File → Open*, choose this `android` folder, wait for the
  sync, then *Run*. Android Studio brings its own JDK and Android SDK.
* Command line (JDK 17 or newer and the Android SDK with platform 35
  installed, `ANDROID_HOME` set):

  ```bash
  ./gradlew assembleDebug
  ```

  The APK is written to `app/build/outputs/apk/debug/app-debug.apk`.

## Install and pair

1. Open the `.apk` on the phone and allow your file manager or browser to
   install apps when Android asks.
2. On the laptop, open Felix and choose to pair a phone. It shows a QR code.
3. Scan the code with the phone's camera app. Felix Companion opens and asks
   you to confirm. (You can also type the address, port, pairing code and
   security fingerprint by hand in the app.)
4. Work through the **Permissions** list in the app. Everything is optional;
   each entry says what it is for.

**"Restricted setting" message (Android 13 and later).** Android blocks
notification access and accessibility for apps installed outside the Play
Store until you lift the block once: long-press the Felix Companion icon →
**App info** → the three-dot menu → **Allow restricted settings**. Then go back
and allow the permission. The app has a button that opens App info.

## What each permission is for

| Permission | Used for | Without it |
|---|---|---|
| Notification access | Reading WhatsApp / Messages / Telegram / Teams / Slack / Signal notifications and replying through their reply button | Felix sees no phone messages and cannot answer them |
| Accessibility (screen control) | Starting a *new* WhatsApp message, Back / Home / lock / screenshot, tapping, typing, reading the screen on request | `wa.send` and all `ui.*` commands report `accessibility_off` |
| Display over other apps | The floating Felix bubble; opening apps, links, alarms and the dialler while Felix is in the background | No bubble; those commands report `permission_denied` unless accessibility is on |
| Unrestricted battery | Keeping the link to the laptop open with the screen off | Android may pause the link after a while |
| Notifications | Showing briefs and messages from Felix | `notify` reports `permission_denied` |
| Contacts | Calling or texting by name, contact search | Numbers only |
| Calendar | Listing and changing events in the calendars synced to the phone | Calendar commands report `permission_denied` |
| SMS | Sending texts, listing the inbox, reporting incoming texts | SMS commands report `permission_denied` |
| Phone | Placing calls directly, missed-call reports, call log | Calls open the dialler instead; no call events |
| Location (all the time) | Answering "where is my phone" | `location` reports `permission_denied` or `unavailable` |
| Microphone | Talking to Felix from the bubble | Typing only |
| Do Not Disturb access | Silent mode and Do Not Disturb | `ringer` / `dnd` report `permission_denied` |
| Modify system settings | Screen brightness | `brightness` reports `permission_denied` |

Only notifications from the apps listed under **Apps Felix may read** are
passed on. Notifications from every other app are ignored on the phone.

## How it stays private

* The connection is HTTPS and **pinned**: at pairing the phone stores the
  SHA-256 fingerprint of your laptop's certificate and refuses any other
  certificate, whatever its name or issuer.
* The pairing token and queued messages are stored in app-private storage and
  are excluded from Android backups and device-to-device transfers.
* Plain HTTP is possible only if you explicitly pair with an `http` link, which
  the app labels as unencrypted and for testing only.
* The accessibility service acts on request only. It does not watch or record
  what you do.

## Honest limits

* **WhatsApp replies depend on a notification.** Felix answers a chat through
  the reply button of that chat's notification. That works for chats whose
  notification is still showing, or that have messaged you since Felix
  Companion last started. If the reply action has expired, the command
  reports `no_reply_action` and works again after the chat's next message.
* **Starting a new WhatsApp message needs the screen.** `wa.send` opens the
  chat in WhatsApp and presses Send through the accessibility service. The
  phone must be unlocked with the screen on (`screen_locked` otherwise), and it
  briefly shows WhatsApp. It relies on WhatsApp's own Send button, so a
  WhatsApp redesign can break it until the app is updated. A scheduled brief
  sent to a locked phone at 7 a.m. will therefore not go out this way; use the
  Felix notification (`notify`) or one of Core's other WhatsApp transports for
  that.
* **Battery savers kill background apps.** Xiaomi, Oppo, Vivo, OnePlus, Huawei
  and Samsung add their own restrictions on top of Android's. If the link
  keeps dropping, allow unrestricted battery use, enable auto-start for Felix
  Companion and lock it in the recent-apps screen. See
  [dontkillmyapp.com](https://dontkillmyapp.com) for per-brand steps.
* **The laptop must be reachable.** The phone connects to the laptop, on the
  same Wi-Fi or over a private network such as Tailscale. While it cannot, the
  app keeps collecting events (up to 5,000; plain notifications are dropped
  first, messages last) and the bubble only handles phone actions.
* **Messages are read from notifications.** Felix sees what the notification
  shows: text, sender and chat name. It does not see message history, media,
  or chats you have muted. Group and one-to-one chats are told apart by what
  the messaging app reports.
* **Calendar writes go through the phone's calendar app data.** They sync to
  Google or Exchange on the phone's normal schedule. Attendees are added to the
  event, but whether invitations are e-mailed is up to the account's sync
  adapter. Changing a recurring event changes the whole series.
* **Reading the clipboard** is only allowed by Android while Felix Companion
  or its panel is in front.
* **Not tested on a device in this repository's build environment.** The code
  is compiled and its logic and network layer are unit-tested (see below), but
  behaviour that depends on a real phone - notification formats of each
  messenger, the WhatsApp send flow, manufacturer quirks - has to be confirmed
  on your phone.

## For developers

```
app/src/main/java/ai/felix/companion/
  core/       Pure Kotlin, no Android imports: JSON codec, pairing link, wire
              format, chat keys, backoff, queue policy, command ledger, SSE
              parser, name matching, offline command parser
  net/        Pinned-certificate HTTP client (also free of Android imports)
  link/       Foreground service (long poll, uploads), command executor,
              boot and SMS receivers
  notify/     Notification listener and inline-reply cache
  a11y/       Accessibility service (screen control, WhatsApp send flow)
  device/     Calendar, contacts, telephony, location, controls, status
  overlay/    Floating bubble and chat panel
  ui/         Main screen, built in code (no layout XML)
app/src/test/ JVM unit tests for core/ and net/
```

The protocol the app implements is specified in
[`docs/PHONE_PROTOCOL.md`](../docs/PHONE_PROTOCOL.md).

Run the unit tests:

```bash
./gradlew testDebugUnitTest
```

They cover the JSON codec, pairing-link validation, request and response
shapes, chat-key rules, backoff, queue trimming, command de-duplication, the
event-stream parser, the offline command parser, and the HTTP client against a
real TLS server on the loopback interface (certificate pinning, bearer token,
address fail-over, streamed replies).

No Android SDK at hand? `tools/typecheck` type-checks every source file against
the Android 15 framework classes, validates resources and the manifest, and
runs the same unit tests, using only Maven Central:

```bash
gradle -p tools/typecheck check
```

It does not build an APK; see `tools/typecheck/README.md` for what it can and
cannot prove.

Versions are pinned in `build.gradle.kts` and `gradle/wrapper`: Gradle 8.14.3,
Android Gradle Plugin 8.13.0, Kotlin 2.3.21, compile and target SDK 35,
minimum SDK 26 (Android 8.0).
