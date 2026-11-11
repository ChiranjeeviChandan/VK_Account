package ai.felix.companion.notify

import ai.felix.companion.Hub
import ai.felix.companion.core.ChatKeys
import ai.felix.companion.core.EventTypes
import ai.felix.companion.core.RecentSet
import ai.felix.companion.link.FelixLinkService
import android.app.Notification
import android.app.Person
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification

/**
 * Reads notifications from the apps the user allowed and turns them into
 * protocol events. Chat notifications become `message` events (one per
 * message, never repeated); everything else becomes a `notification` event.
 */
class FelixNotificationListener : NotificationListenerService() {
    private class ChatMessage(val sender: String?, val text: String, val time: Long)

    private val seenMessages = RecentSet(4_000)
    private val seenNotifications = RecentSet(1_500)
    private val reportedKeys = RecentSet(1_500)

    override fun onListenerConnected() {
        instance = this
        val active = try {
            activeNotifications
        } catch (_: RuntimeException) {
            null
        }
        active?.forEach { safely { handlePosted(it) } }
        FelixLinkService.startIfNeeded(this)
    }

    override fun onListenerDisconnected() {
        if (instance === this) instance = null
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn != null) safely { handlePosted(sbn) }
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?) {
        val key = sbn?.key ?: return
        safely {
            if (reportedKeys.remove(key)) {
                Hub.emit(this, EventTypes.NOTIFICATION_REMOVED, mapOf("key" to key))
            }
        }
    }

    private inline fun safely(block: () -> Unit) {
        try {
            block()
        } catch (_: RuntimeException) {
            // Another app's malformed notification must never crash the listener.
        }
    }

    private fun handlePosted(sbn: StatusBarNotification) {
        val pkg = sbn.packageName ?: return
        if (pkg == packageName) return
        if (pkg !in Hub.prefs(this).notificationApps) return
        val notification = sbn.notification ?: return
        if (notification.flags and Notification.FLAG_GROUP_SUMMARY != 0) return
        if (sbn.isOngoing || notification.flags and Notification.FLAG_ONGOING_EVENT != 0) return
        val extras = notification.extras ?: return
        if (extras.getInt(Notification.EXTRA_PROGRESS_MAX, 0) > 0 || extras.getBoolean(Notification.EXTRA_PROGRESS_INDETERMINATE, false)) return

        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString()?.trim().orEmpty()
        val text = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()?.trim().orEmpty()
        val bigText = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()?.trim().orEmpty()

        val messages = readMessages(extras)
        if (messages.isNotEmpty()) {
            emitConversation(sbn, notification, extras, pkg, title, messages)
            return
        }
        if (title.isEmpty() && text.isEmpty()) return
        if (ChatKeys.isMessagingPackage(pkg) && notification.category == Notification.CATEGORY_MESSAGE && text.isNotEmpty()) {
            emitPlainMessage(sbn, notification, pkg, title, if (bigText.isNotEmpty()) bigText else text)
            return
        }
        if (!seenNotifications.add("${sbn.key}\u0001$title\u0001$text")) return
        reportedKeys.add(sbn.key)
        Hub.emit(
            this, EventTypes.NOTIFICATION,
            linkedMapOf(
                "package" to pkg,
                "app" to appLabel(pkg),
                "key" to sbn.key,
                "title" to title,
                "text" to text,
                "big_text" to bigText,
                "category" to (notification.category ?: ""),
                "post_ts" to sbn.postTime,
                "ongoing" to false,
            )
        )
    }

    /** The unread messages carried by a MessagingStyle notification, oldest first. */
    private fun readMessages(extras: Bundle): List<ChatMessage> {
        @Suppress("DEPRECATION")
        val bundles = extras.getParcelableArray(Notification.EXTRA_MESSAGES) ?: return emptyList()
        val out = ArrayList<ChatMessage>(bundles.size)
        for (item in bundles) {
            val bundle = item as? Bundle ?: continue
            val text = bundle.getCharSequence("text")?.toString()?.trim().orEmpty()
            if (text.isEmpty()) continue
            out.add(ChatMessage(senderOf(bundle), text, bundle.getLong("time", 0L)))
        }
        return out
    }

    private fun senderOf(message: Bundle): String? {
        if (Build.VERSION.SDK_INT >= 28) {
            @Suppress("DEPRECATION")
            val person = message.getParcelable<Person>("sender_person")
            val name = person?.name?.toString()?.trim()
            if (!name.isNullOrEmpty()) return name
        }
        return message.getCharSequence("sender")?.toString()?.trim()?.takeIf { it.isNotEmpty() }
    }

    /** The name the posting app uses for the phone's owner, so their own replies are not reported back. */
    private fun selfName(extras: Bundle): String? {
        if (Build.VERSION.SDK_INT >= 28) {
            @Suppress("DEPRECATION")
            val me = extras.getParcelable<Person>(Notification.EXTRA_MESSAGING_PERSON)
            val name = me?.name?.toString()?.trim()
            if (!name.isNullOrEmpty()) return name
        }
        @Suppress("DEPRECATION") // still what apps targeting Android 8 and 9 fill in
        val legacy = extras.getCharSequence(Notification.EXTRA_SELF_DISPLAY_NAME)
        return legacy?.toString()?.trim()?.takeIf { it.isNotEmpty() }
    }

    private fun emitConversation(
        sbn: StatusBarNotification,
        notification: Notification,
        extras: Bundle,
        pkg: String,
        title: String,
        messages: List<ChatMessage>,
    ) {
        val me = selfName(extras)
        val incoming = messages.filter { it.sender != null && it.sender != me }
        val conversationTitle = extras.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE)?.toString()?.trim().orEmpty()
        val rawName = when {
            conversationTitle.isNotEmpty() -> conversationTitle
            title.isNotEmpty() -> title
            else -> incoming.firstOrNull()?.sender ?: "Unknown"
        }
        val chatName = ChatKeys.cleanTitle(rawName)
        val flaggedGroup = extras.getBoolean(Notification.EXTRA_IS_GROUP_CONVERSATION, false)
        val isGroup = flaggedGroup || (conversationTitle.isNotEmpty() && incoming.any { it.sender != chatName })
        val chatKey = ChatKeys.chatKey(pkg, notification.shortcutId, sbn.tag, chatName)

        val reply = ReplyActions.findReplyAction(notification)
        if (reply != null) ReplyActions.remember(chatKey, reply)

        val channel = ChatKeys.channelFor(pkg)
        var emitted = false
        for (message in incoming) {
            val sender = message.sender ?: continue
            val time = if (message.time > 0) message.time else sbn.postTime
            if (!seenMessages.add(ChatKeys.messageFingerprint(chatKey, sender, message.text, time))) continue
            emitMessage(sbn, pkg, channel, chatKey, chatName, isGroup, sender, message.text, time, reply != null)
            emitted = true
        }
        if (emitted) reportedKeys.add(sbn.key)
    }

    /** A chat notification that does not use MessagingStyle: one message, sender taken from the title. */
    private fun emitPlainMessage(sbn: StatusBarNotification, notification: Notification, pkg: String, title: String, text: String) {
        val split = ChatKeys.splitGroupTitle(title)
        val chatName = ChatKeys.cleanTitle(split?.first ?: title).ifEmpty { "Unknown" }
        val sender = split?.second ?: chatName
        val chatKey = ChatKeys.chatKey(pkg, notification.shortcutId, sbn.tag, chatName)
        val reply = ReplyActions.findReplyAction(notification)
        if (reply != null) ReplyActions.remember(chatKey, reply)
        val time = if (notification.`when` > 0) notification.`when` else sbn.postTime
        if (!seenMessages.add(ChatKeys.messageFingerprint(chatKey, sender, text, time))) return
        emitMessage(sbn, pkg, ChatKeys.channelFor(pkg), chatKey, chatName, split != null, sender, text, time, reply != null)
        reportedKeys.add(sbn.key)
    }

    private fun emitMessage(
        sbn: StatusBarNotification,
        pkg: String,
        channel: String,
        chatKey: String,
        chatName: String,
        isGroup: Boolean,
        sender: String,
        text: String,
        time: Long,
        canReply: Boolean,
    ) {
        Hub.emit(
            this, EventTypes.MESSAGE,
            linkedMapOf(
                "channel" to channel,
                "package" to pkg,
                "chat_key" to chatKey,
                "chat_name" to chatName,
                "is_group" to isGroup,
                "sender" to sender,
                "text" to text,
                "msg_ts" to time,
                "can_reply" to canReply,
                "notification_key" to sbn.key,
            ),
            id = ChatKeys.messageEventId(chatKey, sender, text, time)
        )
    }

    private fun appLabel(pkg: String): String = try {
        packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
    } catch (_: PackageManager.NameNotFoundException) {
        pkg
    }

    /** Current notifications from allowed apps, in the shape of `notification` events. */
    fun snapshot(): List<Map<String, Any?>> {
        val allowed = Hub.prefs(this).notificationApps
        val active = try {
            activeNotifications
        } catch (_: RuntimeException) {
            null
        } ?: return emptyList()
        val out = ArrayList<Map<String, Any?>>()
        for (sbn in active) {
            val pkg = sbn.packageName ?: continue
            if (pkg == packageName || pkg !in allowed) continue
            val notification = sbn.notification ?: continue
            if (notification.flags and Notification.FLAG_GROUP_SUMMARY != 0) continue
            val extras = notification.extras ?: continue
            out.add(
                linkedMapOf(
                    "package" to pkg,
                    "app" to appLabel(pkg),
                    "key" to sbn.key,
                    "title" to (extras.getCharSequence(Notification.EXTRA_TITLE)?.toString() ?: ""),
                    "text" to (extras.getCharSequence(Notification.EXTRA_TEXT)?.toString() ?: ""),
                    "big_text" to (extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString() ?: ""),
                    "category" to (notification.category ?: ""),
                    "post_ts" to sbn.postTime,
                    "ongoing" to sbn.isOngoing,
                )
            )
        }
        return out
    }

    fun dismiss(key: String): Boolean = try {
        cancelNotification(key)
        true
    } catch (_: RuntimeException) {
        false
    }

    companion object {
        /** The connected listener, or null while Android has it unbound. */
        @Volatile
        var instance: FelixNotificationListener? = null
            private set
    }
}
