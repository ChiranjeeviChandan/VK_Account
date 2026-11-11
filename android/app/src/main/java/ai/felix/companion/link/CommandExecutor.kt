package ai.felix.companion.link

import ai.felix.companion.a11y.FelixAccessibilityService
import ai.felix.companion.core.Command
import ai.felix.companion.core.Errors
import ai.felix.companion.core.Outcome
import ai.felix.companion.core.ParsedCommand
import ai.felix.companion.core.bool
import ai.felix.companion.core.double
import ai.felix.companion.core.int
import ai.felix.companion.core.long
import ai.felix.companion.core.str
import ai.felix.companion.device.CalendarBridge
import ai.felix.companion.device.ContactsBridge
import ai.felix.companion.device.Controls
import ai.felix.companion.device.DeviceStatus
import ai.felix.companion.device.LocationBridge
import ai.felix.companion.device.Locator
import ai.felix.companion.device.Speaker
import ai.felix.companion.device.TelephonyBridge
import ai.felix.companion.notify.FelixNotificationListener
import ai.felix.companion.notify.ReplyActions
import android.content.Context

/**
 * Runs one protocol command and reports a protocol outcome. Must be called
 * from a background thread: several commands wait for the screen or a sensor.
 */
class CommandExecutor(context: Context) {
    private val context: Context = context.applicationContext

    fun execute(command: Command, nowMs: Long = System.currentTimeMillis()): Outcome {
        if (command.isExpired(nowMs)) return Outcome.fail(Errors.EXPIRED, "The command arrived after its deadline.")
        return try {
            run(command.type, command.args)
        } catch (e: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, e.message)
        } catch (e: RuntimeException) {
            Outcome.fail(Errors.FAILED, e.message ?: e.javaClass.simpleName)
        }
    }

    fun run(type: String, args: Map<String, Any?>): Outcome = when (type) {
        "ping" -> Outcome.ok("pong" to true)
        "status" -> Outcome.ok(DeviceStatus.collect(context))
        "notify" -> {
            val title = args.str("title")?.trim().orEmpty()
            val text = args.str("text").orEmpty()
            if (title.isEmpty() && text.isEmpty()) {
                Outcome.fail(Errors.BAD_ARGS, "title or text is required.")
            } else {
                Controls.notify(context, title.ifEmpty { "Felix" }, text, args.str("big_text"), args.str("tag"))
            }
        }
        "wa.reply" -> ReplyActions.send(context, args.str("chat_key").orEmpty(), args.str("text").orEmpty())
        "wa.send" -> accessibility { it.sendWhatsApp(args.str("number").orEmpty(), args.str("text").orEmpty(), args.str("package") ?: "com.whatsapp") }
        "sms.send" -> TelephonyBridge.sendSms(context, args.str("to").orEmpty(), args.str("text").orEmpty())
        "call.dial" -> TelephonyBridge.dial(context, args.str("number").orEmpty())
        "app.open" -> Controls.openApp(context, args.str("name"), args.str("package"))
        "url.open" -> Controls.openUrl(context, args.str("url").orEmpty())
        "alarm.set" -> {
            val hour = args.int("hour")
            val minute = args.int("minute")
            if (hour == null || minute == null) Outcome.fail(Errors.BAD_ARGS, "hour and minute are required.")
            else Controls.setAlarm(context, hour, minute, args.str("label"))
        }
        "timer.set" -> {
            val seconds = args.int("seconds")
            if (seconds == null) Outcome.fail(Errors.BAD_ARGS, "seconds is required.")
            else Controls.setTimer(context, seconds, args.str("label"))
        }
        "torch" -> if (!args.containsKey("on")) Outcome.fail(Errors.BAD_ARGS, "on is required.") else Controls.torch(context, args.bool("on"))
        "volume" -> {
            val level = args.int("level")
            if (level == null) Outcome.fail(Errors.BAD_ARGS, "level is required.")
            else Controls.volume(context, args.str("stream") ?: "media", level)
        }
        "ringer" -> Controls.ringer(context, args.str("mode").orEmpty())
        "dnd" -> if (!args.containsKey("on")) Outcome.fail(Errors.BAD_ARGS, "on is required.") else Controls.doNotDisturb(context, args.bool("on"))
        "brightness" -> {
            val level = args.int("level")
            if (level == null) Outcome.fail(Errors.BAD_ARGS, "level is required.") else Controls.brightness(context, level)
        }
        "locate" -> Locator.start(context, args.int("seconds") ?: 30)
        "location" -> LocationBridge.current(context)
        "contacts.search" -> ContactsBridge.search(context, args.str("query").orEmpty(), args.int("limit") ?: 10)
        "contacts.dump" -> ContactsBridge.dump(context, args.int("limit") ?: 2_000)
        "calendar.calendars" -> CalendarBridge.calendars(context)
        "calendar.list" -> {
            val start = args.long("start_ms")
            val end = args.long("end_ms")
            if (start == null || end == null) Outcome.fail(Errors.BAD_ARGS, "start_ms and end_ms are required.")
            else CalendarBridge.list(context, start, end)
        }
        "calendar.create" -> CalendarBridge.create(context, args)
        "calendar.update" -> CalendarBridge.update(context, args)
        "calendar.delete" -> CalendarBridge.delete(context, args.str("id"))
        "ui.global" -> accessibility { it.global(args.str("action").orEmpty()) }
        "ui.click" -> accessibility { it.click(args.str("text"), args.str("id"), args.str("desc"), args.int("index") ?: 0) }
        "ui.type" -> accessibility { it.type(args.str("text").orEmpty(), args.bool("submit")) }
        "ui.scroll" -> accessibility { it.scroll(args.str("direction").orEmpty()) }
        "ui.swipe" -> {
            val x1 = args.double("x1")
            val y1 = args.double("y1")
            val x2 = args.double("x2")
            val y2 = args.double("y2")
            if (x1 == null || y1 == null || x2 == null || y2 == null) {
                Outcome.fail(Errors.BAD_ARGS, "x1, y1, x2 and y2 are required.")
            } else {
                accessibility { it.swipe(x1.toFloat(), y1.toFloat(), x2.toFloat(), y2.toFloat(), args.long("ms") ?: 300L) }
            }
        }
        "ui.dump" -> accessibility { it.dump() }
        "clipboard.set" -> Controls.setClipboard(context, args.str("text").orEmpty())
        "clipboard.get" -> Controls.getClipboard(context)
        "tts" -> {
            val text = args.str("text")?.trim().orEmpty()
            if (text.isEmpty()) {
                Outcome.fail(Errors.BAD_ARGS, "text is required.")
            } else {
                Speaker.speak(context, text)
                Outcome.ok()
            }
        }
        "media" -> Controls.media(context, args.str("action").orEmpty())
        "notifications.list" -> listener { Outcome.ok("notifications" to it.snapshot()) }
        "notification.dismiss" -> listener {
            val key = args.str("key").orEmpty()
            when {
                key.isEmpty() -> Outcome.fail(Errors.BAD_ARGS, "key is required.")
                it.dismiss(key) -> Outcome.ok()
                else -> Outcome.fail(Errors.FAILED, "The notification could not be dismissed.")
            }
        }
        "sms.list" -> TelephonyBridge.listSms(context, args.int("limit") ?: 50, args.long("since_ms"))
        "calls.list" -> TelephonyBridge.listCalls(context, args.int("limit") ?: 50)
        else -> Outcome.fail(Errors.UNSUPPORTED, "This version of Felix Companion does not know \"$type\".")
    }

    private inline fun accessibility(block: (FelixAccessibilityService) -> Outcome): Outcome {
        val service = FelixAccessibilityService.instance
            ?: return Outcome.fail(Errors.ACCESSIBILITY_OFF, "The Felix accessibility service is not enabled on the phone.")
        return block(service)
    }

    private inline fun listener(block: (FelixNotificationListener) -> Outcome): Outcome {
        val service = FelixNotificationListener.instance
            ?: return Outcome.fail(Errors.PERMISSION_DENIED, "Felix does not have notification access on the phone.")
        return block(service)
    }

    /**
     * Runs a command typed into the overlay while Core is unreachable and
     * returns the sentence to show. Names are resolved against the address
     * book first; Felix never guesses between two people.
     */
    fun runLocal(parsed: ParsedCommand): String {
        var args = parsed.args
        val name = args.str("name")
        if (name != null && (parsed.type == "call.dial" || parsed.type == "sms.send")) {
            when (val who = ContactsBridge.resolveNumber(context, name)) {
                is ContactsBridge.Resolution.Found -> {
                    val key = if (parsed.type == "call.dial") "number" else "to"
                    args = args - "name" + (key to who.number)
                }
                is ContactsBridge.Resolution.Ambiguous ->
                    return "Several contacts match \"$name\": ${who.names.take(5).joinToString(", ")}. Say the full name."
                is ContactsBridge.Resolution.Missing -> return who.reason
            }
        }
        val outcome = try {
            run(parsed.type, args)
        } catch (e: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, e.message)
        } catch (e: RuntimeException) {
            Outcome.fail(Errors.FAILED, e.message ?: e.javaClass.simpleName)
        }
        if (!outcome.ok) return outcome.message ?: "That did not work (${outcome.error})."
        return when (parsed.type) {
            "status" -> DeviceStatus.describe(context)
            "call.dial" -> if (outcome.data["placed"] == false) "The dialler is open with the number filled in." else parsed.summary
            else -> parsed.summary
        }
    }
}
