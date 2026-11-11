package ai.felix.companion

import ai.felix.companion.core.PairingInfo
import ai.felix.companion.net.LinkConfig
import android.content.Context
import android.content.SharedPreferences
import java.util.UUID

/**
 * Everything the companion remembers, kept in app-private preferences that are
 * excluded from backups (see the manifest): the pairing with Felix Core and
 * the user's local settings.
 */
class Prefs(context: Context) : LinkConfig {
    private val store: SharedPreferences =
        context.applicationContext.getSharedPreferences("felix", Context.MODE_PRIVATE)

    val deviceId: String
        get() {
            val existing = store.getString(KEY_DEVICE_ID, null)
            if (!existing.isNullOrEmpty()) return existing
            val created = UUID.randomUUID().toString()
            store.edit().putString(KEY_DEVICE_ID, created).apply()
            return created
        }

    override val token: String? get() = store.getString(KEY_TOKEN, null)?.takeIf { it.isNotEmpty() }
    override val scheme: String get() = store.getString(KEY_SCHEME, "https") ?: "https"
    override val port: Int get() = store.getInt(KEY_PORT, 8766)
    override val fingerprint: String? get() = store.getString(KEY_FINGERPRINT, null)?.takeIf { it.isNotEmpty() }
    val coreName: String get() = store.getString(KEY_CORE_NAME, "Felix") ?: "Felix"
    val pollWait: Int get() = store.getInt(KEY_POLL_WAIT, 25).coerceIn(1, 120)

    override val hosts: List<String>
        get() = (store.getString(KEY_HOSTS, "") ?: "").split('\n').filter { it.isNotBlank() }

    /** The host that answered last; tried first on the next request. */
    override var activeHost: String?
        get() = store.getString(KEY_ACTIVE_HOST, null)?.takeIf { it.isNotEmpty() }
        set(value) {
            if (value != activeHost) store.edit().putString(KEY_ACTIVE_HOST, value ?: "").apply()
        }

    /** Set when Core answered 401: the device was unpaired on the laptop. */
    var revoked: Boolean
        get() = store.getBoolean(KEY_REVOKED, false)
        set(value) = store.edit().putBoolean(KEY_REVOKED, value).apply()

    val isPaired: Boolean get() = token != null && hosts.isNotEmpty()

    /** Paired and not revoked: the link may talk to Core. */
    val canSync: Boolean get() = isPaired && !revoked

    fun savePairing(info: PairingInfo, token: String, workingHost: String, coreName: String, pollWait: Int) {
        store.edit()
            .putString(KEY_TOKEN, token)
            .putString(KEY_SCHEME, info.scheme)
            .putString(KEY_HOSTS, info.hosts.joinToString("\n"))
            .putString(KEY_ACTIVE_HOST, workingHost)
            .putInt(KEY_PORT, info.port)
            .putString(KEY_FINGERPRINT, info.fingerprint ?: "")
            .putString(KEY_CORE_NAME, coreName)
            .putInt(KEY_POLL_WAIT, pollWait)
            .putBoolean(KEY_REVOKED, false)
            .apply()
    }

    fun clearPairing() {
        store.edit()
            .remove(KEY_TOKEN).remove(KEY_HOSTS).remove(KEY_ACTIVE_HOST).remove(KEY_FINGERPRINT)
            .remove(KEY_CORE_NAME).remove(KEY_REVOKED)
            .apply()
    }

    var overlayEnabled: Boolean
        get() = store.getBoolean(KEY_OVERLAY, false)
        set(value) = store.edit().putBoolean(KEY_OVERLAY, value).apply()

    var speakReplies: Boolean
        get() = store.getBoolean(KEY_SPEAK, false)
        set(value) = store.edit().putBoolean(KEY_SPEAK, value).apply()

    var bubbleX: Int
        get() = store.getInt(KEY_BUBBLE_X, -1)
        set(value) = store.edit().putInt(KEY_BUBBLE_X, value).apply()

    var bubbleY: Int
        get() = store.getInt(KEY_BUBBLE_Y, -1)
        set(value) = store.edit().putInt(KEY_BUBBLE_Y, value).apply()

    /** Packages whose notifications are forwarded to Core. */
    var notificationApps: Set<String>
        get() {
            val saved = store.getString(KEY_APPS, null) ?: return DEFAULT_APPS
            return saved.split('\n').filter { it.isNotBlank() }.toSet()
        }
        set(value) = store.edit().putString(KEY_APPS, value.sorted().joinToString("\n")).apply()

    companion object {
        private const val KEY_DEVICE_ID = "device_id"
        private const val KEY_TOKEN = "token"
        private const val KEY_SCHEME = "scheme"
        private const val KEY_HOSTS = "hosts"
        private const val KEY_ACTIVE_HOST = "active_host"
        private const val KEY_PORT = "port"
        private const val KEY_FINGERPRINT = "fingerprint"
        private const val KEY_CORE_NAME = "core_name"
        private const val KEY_POLL_WAIT = "poll_wait"
        private const val KEY_REVOKED = "revoked"
        private const val KEY_OVERLAY = "overlay"
        private const val KEY_SPEAK = "speak"
        private const val KEY_BUBBLE_X = "bubble_x"
        private const val KEY_BUBBLE_Y = "bubble_y"
        private const val KEY_APPS = "notification_apps"

        /** Same defaults as `phone.notification_apps` in Felix Core's configuration. */
        val DEFAULT_APPS: Set<String> = setOf(
            "com.whatsapp",
            "com.whatsapp.w4b",
            "com.google.android.apps.messaging",
            "com.samsung.android.messaging",
            "com.microsoft.teams",
            "com.Slack",
            "org.telegram.messenger",
            "org.thoughtcrime.securesms",
        )
    }
}
