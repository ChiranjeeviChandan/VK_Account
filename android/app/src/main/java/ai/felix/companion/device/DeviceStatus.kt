package ai.felix.companion.device

import android.app.KeyguardManager
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.PowerManager

/** Builds the `status` object of the phone protocol. */
object DeviceStatus {
    fun collect(context: Context): Map<String, Any?> {
        val app = context.applicationContext
        return linkedMapOf(
            "battery" to batteryLevel(app),
            "charging" to isCharging(app),
            "network" to network(app),
            "ringer" to ringer(app),
            "dnd" to doNotDisturb(app),
            "screen_on" to screenOn(app),
            "locked" to isLocked(app),
            "permissions" to Permissions.protocolMap(app),
        )
    }

    fun batteryLevel(context: Context): Int {
        val manager = context.getSystemService(BatteryManager::class.java)
        val direct = manager?.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY) ?: -1
        if (direct in 0..100) return direct
        val sticky = context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED)) ?: return -1
        val level = sticky.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = sticky.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
        return if (level >= 0 && scale > 0) level * 100 / scale else -1
    }

    fun isCharging(context: Context): Boolean =
        context.getSystemService(BatteryManager::class.java)?.isCharging ?: false

    fun network(context: Context): String {
        val manager = context.getSystemService(ConnectivityManager::class.java) ?: return "none"
        val capabilities = try {
            val active = manager.activeNetwork ?: return "none"
            manager.getNetworkCapabilities(active) ?: return "none"
        } catch (_: SecurityException) {
            return "none"
        }
        return when {
            capabilities.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) &&
                !capabilities.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> "cellular"
            capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) -> "wifi"
            else -> "none"
        }
    }

    fun hasNetwork(context: Context): Boolean = network(context) != "none"

    fun ringer(context: Context): String =
        when (context.getSystemService(AudioManager::class.java)?.ringerMode) {
            AudioManager.RINGER_MODE_SILENT -> "silent"
            AudioManager.RINGER_MODE_VIBRATE -> "vibrate"
            else -> "normal"
        }

    fun doNotDisturb(context: Context): Boolean {
        val filter = context.getSystemService(NotificationManager::class.java)?.currentInterruptionFilter
            ?: return false
        return filter != NotificationManager.INTERRUPTION_FILTER_ALL &&
            filter != NotificationManager.INTERRUPTION_FILTER_UNKNOWN
    }

    fun screenOn(context: Context): Boolean =
        context.getSystemService(PowerManager::class.java)?.isInteractive ?: false

    fun isLocked(context: Context): Boolean =
        context.getSystemService(KeyguardManager::class.java)?.isKeyguardLocked ?: false

    /** One line for the overlay, e.g. "Battery 82% (charging), Wi-Fi, ringer normal". */
    fun describe(context: Context): String {
        val level = batteryLevel(context)
        val battery = if (level >= 0) "Battery $level%" else "Battery unknown"
        val charging = if (isCharging(context)) " (charging)" else ""
        val net = when (network(context)) {
            "wifi" -> "Wi-Fi"
            "cellular" -> "mobile data"
            else -> "no network"
        }
        val dnd = if (doNotDisturb(context)) ", Do Not Disturb on" else ""
        return "$battery$charging, $net, ringer ${ringer(context)}$dnd."
    }
}
