package ai.felix.companion.device

import ai.felix.companion.a11y.FelixAccessibilityService
import ai.felix.companion.notify.FelixNotificationListener
import android.Manifest
import android.app.NotificationManager
import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.PowerManager
import android.provider.Settings

/** One place that answers "is Felix allowed to do X on this phone right now?". */
object Permissions {
    fun has(context: Context, permission: String): Boolean =
        context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED

    fun hasAll(context: Context, vararg permissions: String): Boolean = permissions.all { has(context, it) }

    fun notificationListener(context: Context): Boolean {
        val component = ComponentName(context, FelixNotificationListener::class.java)
        if (Build.VERSION.SDK_INT >= 27) {
            val manager = context.getSystemService(NotificationManager::class.java)
            if (manager != null) return manager.isNotificationListenerAccessGranted(component)
        }
        val enabled = Settings.Secure.getString(context.contentResolver, "enabled_notification_listeners") ?: return false
        return enabled.split(':').any { ComponentName.unflattenFromString(it) == component }
    }

    fun accessibility(context: Context): Boolean {
        val component = ComponentName(context, FelixAccessibilityService::class.java)
        val enabled = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
            ?: return false
        return enabled.split(':').any { ComponentName.unflattenFromString(it) == component }
    }

    fun overlay(context: Context): Boolean = Settings.canDrawOverlays(context)

    fun batteryUnrestricted(context: Context): Boolean {
        val power = context.getSystemService(PowerManager::class.java) ?: return false
        return power.isIgnoringBatteryOptimizations(context.packageName)
    }

    fun doNotDisturbAccess(context: Context): Boolean {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return false
        return manager.isNotificationPolicyAccessGranted
    }

    fun writeSettings(context: Context): Boolean = Settings.System.canWrite(context)

    fun postNotifications(context: Context): Boolean {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return false
        if (!manager.areNotificationsEnabled()) return false
        return Build.VERSION.SDK_INT < 33 || has(context, Manifest.permission.POST_NOTIFICATIONS)
    }

    fun contacts(context: Context): Boolean = has(context, Manifest.permission.READ_CONTACTS)

    fun calendar(context: Context): Boolean =
        hasAll(context, Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR)

    fun sms(context: Context): Boolean = has(context, Manifest.permission.SEND_SMS)

    fun call(context: Context): Boolean = has(context, Manifest.permission.CALL_PHONE)

    fun location(context: Context): Boolean =
        has(context, Manifest.permission.ACCESS_FINE_LOCATION) || has(context, Manifest.permission.ACCESS_COARSE_LOCATION)

    fun backgroundLocation(context: Context): Boolean =
        Build.VERSION.SDK_INT < 29 || has(context, Manifest.permission.ACCESS_BACKGROUND_LOCATION)

    fun microphone(context: Context): Boolean = has(context, Manifest.permission.RECORD_AUDIO)

    /**
     * Android only lets a background app open another app's screen when it
     * holds "display over other apps" or has an active accessibility service.
     */
    fun canOpenScreens(context: Context): Boolean = overlay(context) || FelixAccessibilityService.instance != null

    /** The `permissions` object of the protocol's `status` event. */
    fun protocolMap(context: Context): Map<String, Any?> = linkedMapOf(
        "notifications" to notificationListener(context),
        "accessibility" to accessibility(context),
        "overlay" to overlay(context),
        "sms" to sms(context),
        "contacts" to contacts(context),
        "calendar" to calendar(context),
        "call" to call(context),
        "location" to location(context),
    )
}
