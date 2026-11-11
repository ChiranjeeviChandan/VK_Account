package ai.felix.companion.device

import ai.felix.companion.core.Errors
import ai.felix.companion.core.Matching
import ai.felix.companion.core.Outcome
import android.Manifest
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.CallLog
import android.provider.Telephony
import android.telecom.TelecomManager
import android.telephony.SmsManager

/** SMS and phone calls. */
object TelephonyBridge {
    fun sendSms(context: Context, to: String, text: String): Outcome {
        val number = to.trim()
        if (number.isEmpty() || text.isEmpty()) return Outcome.fail(Errors.BAD_ARGS, "to and text are required.")
        if (!Permissions.has(context, Manifest.permission.SEND_SMS)) {
            return Outcome.fail(Errors.PERMISSION_DENIED, "Felix is not allowed to send SMS.")
        }
        return try {
            @Suppress("DEPRECATION")
            val manager: SmsManager? =
                if (Build.VERSION.SDK_INT >= 31) context.getSystemService(SmsManager::class.java) else SmsManager.getDefault()
            if (manager == null) {
                Outcome.fail(Errors.UNAVAILABLE, "This device cannot send SMS.")
            } else {
                val parts = manager.divideMessage(text)
                if (parts.size <= 1) {
                    manager.sendTextMessage(number, null, text, null, null)
                } else {
                    manager.sendMultipartTextMessage(number, null, parts, null, null)
                }
                Outcome.ok("parts" to parts.size.coerceAtLeast(1))
            }
        } catch (_: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, "Felix is not allowed to send SMS.")
        } catch (e: IllegalArgumentException) {
            Outcome.fail(Errors.BAD_ARGS, e.message)
        } catch (e: RuntimeException) {
            Outcome.fail(Errors.UNAVAILABLE, e.message)
        }
    }

    /** Places the call when permitted, otherwise opens the dialler with the number filled in. */
    fun dial(context: Context, rawNumber: String): Outcome {
        val number = rawNumber.trim()
        if (!Matching.looksLikeNumber(number)) return Outcome.fail(Errors.BAD_ARGS, "\"$rawNumber\" is not a phone number.")
        val uri = Uri.fromParts("tel", Matching.cleanNumber(number), null)
        if (Permissions.has(context, Manifest.permission.CALL_PHONE)) {
            val telecom = context.getSystemService(TelecomManager::class.java)
            if (telecom != null) {
                try {
                    telecom.placeCall(uri, Bundle())
                    return Outcome.ok("placed" to true)
                } catch (_: SecurityException) {
                    // Fall through to the dialler.
                } catch (_: RuntimeException) {
                    // Some devices have no calling account; the dialler can still explain why.
                }
            }
        }
        if (!Permissions.canOpenScreens(context)) {
            return Outcome.fail(
                Errors.PERMISSION_DENIED,
                "Grant the Phone permission so Felix can place calls, or allow \"Display over other apps\" so it can open the dialler."
            )
        }
        return try {
            context.startActivity(Intent(Intent.ACTION_DIAL, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            Outcome.ok("placed" to false)
        } catch (_: ActivityNotFoundException) {
            Outcome.fail(Errors.UNAVAILABLE, "This device has no dialler.")
        } catch (e: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, e.message)
        }
    }

    fun listSms(context: Context, limit: Int, sinceMs: Long?): Outcome {
        if (!Permissions.has(context, Manifest.permission.READ_SMS)) {
            return Outcome.fail(Errors.PERMISSION_DENIED, "Felix is not allowed to read SMS.")
        }
        val wanted = limit.coerceIn(1, 500)
        return try {
            val out = ArrayList<Map<String, Any?>>()
            val selection = if (sinceMs != null) "${Telephony.Sms.DATE} > ?" else null
            val args = if (sinceMs != null) arrayOf(sinceMs.toString()) else null
            context.contentResolver.query(
                Telephony.Sms.Inbox.CONTENT_URI,
                arrayOf(Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE, Telephony.Sms.READ),
                selection, args, "${Telephony.Sms.DATE} DESC"
            )?.use { cursor ->
                while (cursor.moveToNext() && out.size < wanted) {
                    out.add(
                        linkedMapOf(
                            "from" to (cursor.getString(0) ?: ""),
                            "text" to (cursor.getString(1) ?: ""),
                            "ts" to cursor.getLong(2),
                            "read" to (cursor.getInt(3) == 1),
                        )
                    )
                }
            }
            Outcome.ok("messages" to out)
        } catch (_: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, "Felix is not allowed to read SMS.")
        } catch (e: RuntimeException) {
            Outcome.fail(Errors.FAILED, e.message)
        }
    }

    fun listCalls(context: Context, limit: Int): Outcome {
        if (!Permissions.has(context, Manifest.permission.READ_CALL_LOG)) {
            return Outcome.fail(Errors.PERMISSION_DENIED, "Felix is not allowed to read the call log.")
        }
        val wanted = limit.coerceIn(1, 500)
        return try {
            val out = ArrayList<Map<String, Any?>>()
            context.contentResolver.query(
                CallLog.Calls.CONTENT_URI,
                arrayOf(
                    CallLog.Calls.NUMBER, CallLog.Calls.CACHED_NAME, CallLog.Calls.TYPE,
                    CallLog.Calls.DATE, CallLog.Calls.DURATION,
                ),
                null, null, "${CallLog.Calls.DATE} DESC"
            )?.use { cursor ->
                while (cursor.moveToNext() && out.size < wanted) {
                    val type = when (cursor.getInt(2)) {
                        CallLog.Calls.INCOMING_TYPE -> "incoming"
                        CallLog.Calls.OUTGOING_TYPE -> "outgoing"
                        CallLog.Calls.MISSED_TYPE -> "missed"
                        CallLog.Calls.VOICEMAIL_TYPE -> "voicemail"
                        CallLog.Calls.REJECTED_TYPE -> "rejected"
                        CallLog.Calls.BLOCKED_TYPE -> "blocked"
                        else -> "other"
                    }
                    out.add(
                        linkedMapOf(
                            "number" to (cursor.getString(0) ?: ""),
                            "name" to (cursor.getString(1) ?: ""),
                            "type" to type,
                            "ts" to cursor.getLong(3),
                            "duration" to cursor.getLong(4),
                        )
                    )
                }
            }
            Outcome.ok("calls" to out)
        } catch (_: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, "Felix is not allowed to read the call log.")
        } catch (e: RuntimeException) {
            Outcome.fail(Errors.FAILED, e.message)
        }
    }
}
