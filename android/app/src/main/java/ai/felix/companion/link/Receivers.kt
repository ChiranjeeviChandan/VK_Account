package ai.felix.companion.link

import ai.felix.companion.Hub
import ai.felix.companion.core.EventTypes
import ai.felix.companion.device.Permissions
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony
import java.nio.charset.StandardCharsets
import java.util.UUID

/** Brings the link back after a reboot or an app update. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED -> FelixLinkService.startIfNeeded(context)
        }
    }
}

/**
 * Reports incoming SMS as `sms` events with the sender's number. Used when the
 * SMS app's notifications are not being read (no notification access, or the
 * app is not on the allow-list); otherwise the notification listener reports
 * the same message as a `message` event on the `sms` channel.
 */
class SmsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        val prefs = Hub.prefs(context)
        if (!prefs.isPaired) return
        // When Felix already reads the SMS app's notifications, that path reports this
        // message (with the contact's name and a reply action); a second report would
        // show up as a duplicate conversation in Core.
        val smsApp = try {
            Telephony.Sms.getDefaultSmsPackage(context)
        } catch (_: RuntimeException) {
            null
        }
        if (smsApp != null && smsApp in prefs.notificationApps && Permissions.notificationListener(context)) return
        val parts = try {
            Telephony.Sms.Intents.getMessagesFromIntent(intent)
        } catch (_: RuntimeException) {
            null
        } ?: return
        // A long SMS arrives as several parts from the same sender.
        val bodies = LinkedHashMap<String, StringBuilder>()
        var sentAt = 0L
        for (part in parts) {
            if (part == null) continue
            val from = part.displayOriginatingAddress ?: part.originatingAddress ?: continue
            bodies.getOrPut(from) { StringBuilder() }.append(part.displayMessageBody ?: part.messageBody ?: "")
            if (part.timestampMillis > sentAt) sentAt = part.timestampMillis
        }
        if (sentAt == 0L) sentAt = System.currentTimeMillis()
        for ((from, body) in bodies) {
            val text = body.toString()
            val id = UUID.nameUUIDFromBytes("sms\u0001$from\u0001$text\u0001$sentAt".toByteArray(StandardCharsets.UTF_8)).toString()
            Hub.emit(context, EventTypes.SMS, linkedMapOf("from" to from, "text" to text, "msg_ts" to sentAt), id)
        }
        FelixLinkService.startIfNeeded(context)
    }
}
