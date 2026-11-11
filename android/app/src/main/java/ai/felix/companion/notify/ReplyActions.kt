package ai.felix.companion.notify

import ai.felix.companion.core.Errors
import ai.felix.companion.core.Outcome
import android.app.Notification
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Bundle

/**
 * Remembers, per conversation, the inline-reply action of its newest
 * notification. Sending through that action is how Felix answers a WhatsApp
 * (or other messenger) chat without touching the app's own protocol.
 */
object ReplyActions {
    private const val MAX_CHATS = 300

    private class Entry(val intent: PendingIntent, val inputs: Array<RemoteInput>)

    // Access-ordered, so the least recently used conversation is evicted first.
    private val entries = object : LinkedHashMap<String, Entry>(64, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Entry>?): Boolean = size > MAX_CHATS
    }

    /** The action that accepts typed text, preferring one the app marks as "reply". */
    fun findReplyAction(notification: Notification): Notification.Action? {
        val candidates = ArrayList<Notification.Action>()
        notification.actions?.let { candidates.addAll(it) }
        try {
            candidates.addAll(Notification.WearableExtender(notification).actions)
        } catch (_: RuntimeException) {
            // Malformed wearable extras from the posting app: ignore them.
        }
        var fallback: Notification.Action? = null
        for (action in candidates) {
            val inputs = action.remoteInputs ?: continue
            if (action.actionIntent == null || inputs.none { it.allowFreeFormInput }) continue
            if (Build.VERSION.SDK_INT >= 28 && action.semanticAction == Notification.Action.SEMANTIC_ACTION_REPLY) {
                return action
            }
            if (fallback == null) fallback = action
        }
        return fallback
    }

    @Synchronized
    fun remember(chatKey: String, action: Notification.Action) {
        val intent = action.actionIntent ?: return
        val inputs = action.remoteInputs ?: return
        entries[chatKey] = Entry(intent, inputs)
    }

    @Synchronized
    fun canReply(chatKey: String): Boolean = entries.containsKey(chatKey)

    @Synchronized
    private fun lookup(chatKey: String): Entry? = entries[chatKey]

    @Synchronized
    private fun forget(chatKey: String) {
        entries.remove(chatKey)
    }

    @Synchronized
    fun clear() = entries.clear()

    fun send(context: Context, chatKey: String, text: String): Outcome {
        if (chatKey.isBlank() || text.isEmpty()) return Outcome.fail(Errors.BAD_ARGS, "chat_key and text are required.")
        val entry = lookup(chatKey)
            ?: return Outcome.fail(Errors.NO_REPLY_ACTION, "No open notification for that chat.")
        val fill = Intent()
        val results = Bundle()
        for (input in entry.inputs) {
            if (input.allowFreeFormInput) results.putCharSequence(input.resultKey, text)
        }
        RemoteInput.addResultsToIntent(entry.inputs, fill, results)
        return try {
            entry.intent.send(context, 0, fill)
            Outcome.ok()
        } catch (_: PendingIntent.CanceledException) {
            forget(chatKey)
            Outcome.fail(Errors.NO_REPLY_ACTION, "That notification is gone. A reply is possible again after the chat's next message.")
        } catch (e: RuntimeException) {
            Outcome.fail(Errors.FAILED, e.message)
        }
    }
}
