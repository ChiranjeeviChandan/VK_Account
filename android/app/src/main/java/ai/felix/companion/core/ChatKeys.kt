package ai.felix.companion.core

import java.nio.charset.StandardCharsets
import java.util.UUID

/** Naming rules for conversations seen through notifications. */
object ChatKeys {
    private val SMS_PACKAGES = setOf(
        "com.google.android.apps.messaging",
        "com.samsung.android.messaging",
        "com.android.mms",
        "com.android.messaging",
        "com.oneplus.mms",
        "com.miui.mms",
    )

    /** Protocol `channel` value for a package. */
    fun channelFor(pkg: String): String = when {
        pkg == "com.whatsapp" -> "whatsapp"
        pkg == "com.whatsapp.w4b" -> "whatsapp_business"
        pkg in SMS_PACKAGES -> "sms"
        pkg.startsWith("org.telegram.") || pkg == "org.thunderdog.challegram" -> "telegram"
        pkg == "com.microsoft.teams" -> "teams"
        pkg == "com.Slack" -> "slack"
        pkg == "org.thoughtcrime.securesms" -> "signal"
        else -> "other"
    }

    fun isMessagingPackage(pkg: String): Boolean = channelFor(pkg) != "other"

    /**
     * `"<package>|<conversation id>"`, where the conversation id is the
     * notification's shortcut id when present, else its tag, else the
     * conversation title.
     */
    fun chatKey(pkg: String, shortcutId: String?, tag: String?, title: String?): String {
        val conversation = sequenceOf(shortcutId, tag, title?.let { cleanTitle(it) })
            .map { it?.trim().orEmpty() }
            .firstOrNull { it.isNotEmpty() }
            ?: "unknown"
        return "$pkg|$conversation"
    }

    private val COUNT_SUFFIX = Regex("""\s*\(\d+\s+(?:new\s+)?messages?\)\s*$""", RegexOption.IGNORE_CASE)
    private val GROUP_SENDER = Regex("""^(.{1,80}?)\s*:\s+(.{1,60})$""")

    /** Drops decorations some apps add to titles, such as " (3 messages)". */
    fun cleanTitle(title: String): String = COUNT_SUFFIX.replace(title, "").trim()

    /**
     * Older group notifications use "Group name: Sender" as the title. Returns
     * the (group, sender) split, or null when the title has no such shape.
     */
    fun splitGroupTitle(title: String): Pair<String, String>? {
        val match = GROUP_SENDER.matchEntire(cleanTitle(title)) ?: return null
        return match.groupValues[1].trim() to match.groupValues[2].trim()
    }

    /** Identity of one message for de-duplication across notification re-posts. */
    fun messageFingerprint(chatKey: String, sender: String, text: String, msgTs: Long): String =
        "$chatKey\u0001$sender\u0001$text\u0001$msgTs"

    /**
     * Deterministic event id for a message, so that a message re-read after a
     * restart is recognised by Core as one it already has.
     */
    fun messageEventId(chatKey: String, sender: String, text: String, msgTs: Long): String =
        UUID.nameUUIDFromBytes(messageFingerprint(chatKey, sender, text, msgTs).toByteArray(StandardCharsets.UTF_8)).toString()
}
