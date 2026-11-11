package ai.felix.companion.device

import ai.felix.companion.Hub
import ai.felix.companion.R
import ai.felix.companion.core.Errors
import ai.felix.companion.core.Matching
import ai.felix.companion.core.Outcome
import ai.felix.companion.ui.MainActivity
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.hardware.camera2.CameraAccessException
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.provider.AlarmClock
import android.provider.Settings
import android.speech.tts.TextToSpeech
import android.view.KeyEvent
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** Notification channels used by the companion. */
object Channels {
    const val LINK = "felix_link"
    const val FELIX = "felix_messages"

    fun ensure(context: Context) {
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        val link = NotificationChannel(LINK, context.getString(R.string.channel_link), NotificationManager.IMPORTANCE_MIN)
        link.description = context.getString(R.string.channel_link_description)
        link.setShowBadge(false)
        val felix = NotificationChannel(FELIX, context.getString(R.string.channel_felix), NotificationManager.IMPORTANCE_DEFAULT)
        felix.description = context.getString(R.string.channel_felix_description)
        manager.createNotificationChannel(link)
        manager.createNotificationChannel(felix)
    }
}

/** Runs [block] on the main thread and waits for its result (some system services insist on it). */
fun <T> onMainThread(timeoutMs: Long = 3_000, block: () -> T): T? {
    val result = AtomicReference<T?>(null)
    val failure = AtomicReference<RuntimeException?>(null)
    val done = CountDownLatch(1)
    Hub.runOnMain {
        try {
            result.set(block())
        } catch (e: RuntimeException) {
            failure.set(e)
        } finally {
            done.countDown()
        }
    }
    if (!done.await(timeoutMs, TimeUnit.MILLISECONDS)) return null
    failure.get()?.let { throw it }
    return result.get()
}

/** Speaks text aloud through the system text-to-speech engine. */
object Speaker {
    private var engine: TextToSpeech? = null
    private var ready = false
    private val waiting = ArrayList<String>()

    fun speak(context: Context, text: String) {
        val clean = text.trim()
        if (clean.isEmpty()) return
        val app = context.applicationContext
        Hub.runOnMain {
            if (engine == null) {
                engine = TextToSpeech(app) { status ->
                    Hub.runOnMain {
                        if (status == TextToSpeech.SUCCESS) {
                            ready = true
                            try {
                                engine?.language = Locale.getDefault()
                            } catch (_: RuntimeException) {
                                // Keep the engine's own default voice.
                            }
                            val queued = ArrayList(waiting)
                            waiting.clear()
                            queued.forEach { say(it) }
                        } else {
                            engine = null
                            waiting.clear()
                        }
                    }
                }
            }
            if (ready) say(clean) else waiting.add(clean)
        }
    }

    private fun say(text: String) {
        // The engine rejects very long inputs; a spoken reply never needs that much.
        val limit = TextToSpeech.getMaxSpeechInputLength().coerceAtMost(3500)
        engine?.speak(text.take(limit), TextToSpeech.QUEUE_ADD, Bundle(), "felix-" + SystemClock.uptimeMillis())
    }

    fun stop() {
        Hub.runOnMain {
            waiting.clear()
            engine?.stop()
        }
    }

    fun shutdown() {
        Hub.runOnMain {
            engine?.shutdown()
            engine = null
            ready = false
            waiting.clear()
        }
    }
}

/** Rings the phone at full alarm volume so a misplaced phone can be found. */
object Locator {
    private var player: MediaPlayer? = null
    private var previousVolume = -1
    private var generation = 0

    @Synchronized
    fun start(context: Context, seconds: Int): Outcome {
        stopLocked(context)
        val app = context.applicationContext
        val audio = app.getSystemService(AudioManager::class.java)
        val sources = listOfNotNull(
            RingtoneManager.getActualDefaultRingtoneUri(app, RingtoneManager.TYPE_ALARM),
            RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
            RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE),
            RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION),
        )
        var started: MediaPlayer? = null
        for (uri in sources) {
            val candidate = MediaPlayer()
            try {
                candidate.setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_ALARM)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                        .build()
                )
                candidate.setDataSource(app, uri)
                candidate.isLooping = true
                candidate.prepare()
                candidate.start()
                started = candidate
                break
            } catch (_: Exception) {
                candidate.release()
            }
        }
        if (started == null) return Outcome.fail(Errors.UNAVAILABLE, "No alarm sound could be played on this phone.")
        if (audio != null) {
            try {
                previousVolume = audio.getStreamVolume(AudioManager.STREAM_ALARM)
                audio.setStreamVolume(AudioManager.STREAM_ALARM, audio.getStreamMaxVolume(AudioManager.STREAM_ALARM), 0)
            } catch (_: SecurityException) {
                previousVolume = -1
            }
        }
        player = started
        Hub.notifyListeners() // lets the service add a "Stop ringing" button to its notification
        val mine = ++generation
        val duration = seconds.coerceIn(5, 300)
        Hub.postDelayed(duration * 1000L) { stopIfCurrent(app, mine) }
        return Outcome.ok("seconds" to duration)
    }

    @Synchronized
    private fun stopIfCurrent(context: Context, expected: Int) {
        if (generation == expected) stopLocked(context)
    }

    @Synchronized
    fun stop(context: Context) = stopLocked(context)

    val isRinging: Boolean
        @Synchronized get() = player != null

    private fun stopLocked(context: Context) {
        val current = player ?: return
        player = null
        generation++
        try {
            current.stop()
        } catch (_: IllegalStateException) {
        }
        current.release()
        if (previousVolume >= 0) {
            try {
                context.applicationContext.getSystemService(AudioManager::class.java)
                    ?.setStreamVolume(AudioManager.STREAM_ALARM, previousVolume, 0)
            } catch (_: SecurityException) {
            }
            previousVolume = -1
        }
        Hub.notifyListeners()
    }
}

/** Device actions that need no data from other apps. Every function returns a protocol [Outcome]. */
object Controls {
    private const val OPEN_SCREENS_HINT =
        "Android blocked opening a screen from the background. Allow \"Display over other apps\" or enable the Felix accessibility service."

    fun torch(context: Context, on: Boolean): Outcome {
        val cameras = context.getSystemService(CameraManager::class.java)
            ?: return Outcome.fail(Errors.UNAVAILABLE, "This phone has no camera service.")
        return try {
            var chosen: String? = null
            for (id in cameras.cameraIdList) {
                val traits = cameras.getCameraCharacteristics(id)
                if (traits.get(CameraCharacteristics.FLASH_INFO_AVAILABLE) != true) continue
                val facesBack = traits.get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK
                if (chosen == null || facesBack) chosen = id
                if (facesBack) break
            }
            if (chosen == null) {
                Outcome.fail(Errors.UNAVAILABLE, "This phone has no flashlight.")
            } else {
                cameras.setTorchMode(chosen, on)
                Outcome.ok("on" to on)
            }
        } catch (e: CameraAccessException) {
            Outcome.fail(Errors.UNAVAILABLE, "The camera is in use: ${e.message}")
        } catch (e: IllegalArgumentException) {
            Outcome.fail(Errors.UNAVAILABLE, e.message)
        } catch (e: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, e.message)
        }
    }

    fun volume(context: Context, stream: String, level: Int): Outcome {
        val audio = context.getSystemService(AudioManager::class.java)
            ?: return Outcome.fail(Errors.UNAVAILABLE, "No audio service.")
        val type = when (stream) {
            "media" -> AudioManager.STREAM_MUSIC
            "ring" -> AudioManager.STREAM_RING
            "alarm" -> AudioManager.STREAM_ALARM
            "notification" -> AudioManager.STREAM_NOTIFICATION
            else -> return Outcome.fail(Errors.BAD_ARGS, "Unknown stream \"$stream\".")
        }
        val percent = level.coerceIn(0, 100)
        val max = audio.getStreamMaxVolume(type)
        val min = if (Build.VERSION.SDK_INT >= 28) audio.getStreamMinVolume(type) else 0
        val target = min + Math.round((max - min) * percent / 100.0).toInt()
        return try {
            audio.setStreamVolume(type, target, 0)
            Outcome.ok("stream" to stream, "level" to percent)
        } catch (_: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, "Do Not Disturb is on. Grant Felix Do Not Disturb access to change this volume.")
        }
    }

    fun ringer(context: Context, mode: String): Outcome {
        val audio = context.getSystemService(AudioManager::class.java)
            ?: return Outcome.fail(Errors.UNAVAILABLE, "No audio service.")
        val target = when (mode) {
            "normal" -> AudioManager.RINGER_MODE_NORMAL
            "vibrate" -> AudioManager.RINGER_MODE_VIBRATE
            "silent" -> AudioManager.RINGER_MODE_SILENT
            else -> return Outcome.fail(Errors.BAD_ARGS, "Unknown ringer mode \"$mode\".")
        }
        return try {
            audio.ringerMode = target
            Outcome.ok("mode" to mode)
        } catch (_: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, "Changing the ringer needs Do Not Disturb access for Felix.")
        }
    }

    fun doNotDisturb(context: Context, on: Boolean): Outcome {
        val manager = context.getSystemService(NotificationManager::class.java)
            ?: return Outcome.fail(Errors.UNAVAILABLE, "No notification service.")
        if (!manager.isNotificationPolicyAccessGranted) {
            return Outcome.fail(Errors.PERMISSION_DENIED, "Grant Felix Do Not Disturb access first.")
        }
        return try {
            manager.setInterruptionFilter(
                if (on) NotificationManager.INTERRUPTION_FILTER_PRIORITY else NotificationManager.INTERRUPTION_FILTER_ALL
            )
            Outcome.ok("on" to on)
        } catch (e: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, e.message)
        }
    }

    fun brightness(context: Context, level: Int): Outcome {
        if (!Settings.System.canWrite(context)) {
            return Outcome.fail(Errors.PERMISSION_DENIED, "Allow Felix to modify system settings to change brightness.")
        }
        val percent = level.coerceIn(0, 100)
        val resolver = context.contentResolver
        return try {
            Settings.System.putInt(resolver, Settings.System.SCREEN_BRIGHTNESS_MODE, Settings.System.SCREEN_BRIGHTNESS_MODE_MANUAL)
            Settings.System.putInt(resolver, Settings.System.SCREEN_BRIGHTNESS, (percent * 255 / 100).coerceIn(1, 255))
            Outcome.ok("level" to percent)
        } catch (e: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, e.message)
        } catch (e: IllegalArgumentException) {
            Outcome.fail(Errors.FAILED, e.message)
        }
    }

    fun media(context: Context, action: String): Outcome {
        val audio = context.getSystemService(AudioManager::class.java)
            ?: return Outcome.fail(Errors.UNAVAILABLE, "No audio service.")
        val key = when (action) {
            "play_pause" -> KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE
            "next" -> KeyEvent.KEYCODE_MEDIA_NEXT
            "previous" -> KeyEvent.KEYCODE_MEDIA_PREVIOUS
            "stop" -> KeyEvent.KEYCODE_MEDIA_STOP
            else -> return Outcome.fail(Errors.BAD_ARGS, "Unknown media action \"$action\".")
        }
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_DOWN, key))
        audio.dispatchMediaKeyEvent(KeyEvent(KeyEvent.ACTION_UP, key))
        return Outcome.ok("action" to action)
    }

    fun setClipboard(context: Context, text: String): Outcome {
        val done = try {
            onMainThread {
                val clipboard = context.getSystemService(ClipboardManager::class.java)
                if (clipboard == null) {
                    false
                } else {
                    clipboard.setPrimaryClip(ClipData.newPlainText("Felix", text))
                    true
                }
            }
        } catch (e: RuntimeException) {
            return Outcome.fail(Errors.FAILED, e.message)
        }
        return if (done == true) Outcome.ok() else Outcome.fail(Errors.UNAVAILABLE, "The clipboard is not available.")
    }

    fun getClipboard(context: Context): Outcome {
        val text = try {
            onMainThread {
                val clipboard = context.getSystemService(ClipboardManager::class.java)
                val clip = clipboard?.primaryClip
                if (clip == null || clip.itemCount == 0) null else clip.getItemAt(0).coerceToText(context)?.toString()
            }
        } catch (e: RuntimeException) {
            return Outcome.fail(Errors.FAILED, e.message)
        }
        return if (text == null) {
            Outcome.fail(Errors.UNAVAILABLE, "Android only lets Felix read the clipboard while the Felix app or its panel is in front.")
        } else {
            Outcome.ok("text" to text)
        }
    }

    private fun launch(context: Context, intent: Intent, missing: String): Outcome {
        if (!Permissions.canOpenScreens(context)) return Outcome.fail(Errors.PERMISSION_DENIED, OPEN_SCREENS_HINT)
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            context.startActivity(intent)
            Outcome.ok()
        } catch (_: ActivityNotFoundException) {
            Outcome.fail(Errors.UNAVAILABLE, missing)
        } catch (e: SecurityException) {
            Outcome.fail(Errors.PERMISSION_DENIED, e.message)
        }
    }

    fun setAlarm(context: Context, hour: Int, minute: Int, label: String?): Outcome {
        if (hour !in 0..23 || minute !in 0..59) return Outcome.fail(Errors.BAD_ARGS, "The alarm time is not valid.")
        val intent = Intent(AlarmClock.ACTION_SET_ALARM)
            .putExtra(AlarmClock.EXTRA_HOUR, hour)
            .putExtra(AlarmClock.EXTRA_MINUTES, minute)
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
        if (!label.isNullOrBlank()) intent.putExtra(AlarmClock.EXTRA_MESSAGE, label)
        return launch(context, intent, "No clock app accepts alarms on this phone.")
    }

    fun setTimer(context: Context, seconds: Int, label: String?): Outcome {
        if (seconds !in 1..86_400) return Outcome.fail(Errors.BAD_ARGS, "A timer must be between 1 second and 24 hours.")
        val intent = Intent(AlarmClock.ACTION_SET_TIMER)
            .putExtra(AlarmClock.EXTRA_LENGTH, seconds)
            .putExtra(AlarmClock.EXTRA_SKIP_UI, true)
        if (!label.isNullOrBlank()) intent.putExtra(AlarmClock.EXTRA_MESSAGE, label)
        return launch(context, intent, "No clock app accepts timers on this phone.")
    }

    fun openUrl(context: Context, url: String): Outcome {
        val uri = Uri.parse(url.trim())
        val scheme = uri.scheme?.lowercase()
        if (scheme !in setOf("http", "https", "geo", "mailto")) {
            return Outcome.fail(Errors.BAD_ARGS, "Only web, map and mail links can be opened.")
        }
        return launch(context, Intent(Intent.ACTION_VIEW, uri), "No app can open that link.")
    }

    /** Installed apps that have a launcher icon: (label, package). */
    fun launchableApps(context: Context): List<Pair<String, String>> {
        val manager = context.packageManager
        val query = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        @Suppress("DEPRECATION")
        val activities = manager.queryIntentActivities(query, 0)
        return activities
            .mapNotNull { info ->
                val pkg = info.activityInfo?.packageName ?: return@mapNotNull null
                info.loadLabel(manager).toString().ifBlank { pkg } to pkg
            }
            .distinctBy { it.second }
            .sortedBy { it.first.lowercase() }
    }

    fun openApp(context: Context, name: String?, pkg: String?): Outcome {
        val manager = context.packageManager
        var target = pkg?.trim().orEmpty()
        if (target.isEmpty()) {
            val wanted = name?.trim().orEmpty()
            if (wanted.isEmpty()) return Outcome.fail(Errors.BAD_ARGS, "Say which app to open.")
            val apps = launchableApps(context)
            var ranked = Matching.rank(wanted, apps.map { it.first })
            if (!ranked.found) ranked = Matching.rank(wanted, apps.map { it.second.substringAfterLast('.') })
            if (!ranked.found) return Outcome.fail(Errors.NOT_FOUND, "No installed app is called \"$wanted\".")
            target = apps[ranked.best].second
        }
        val intent = manager.getLaunchIntentForPackage(target)
            ?: return Outcome.fail(Errors.NOT_FOUND, "\"$target\" is not installed or cannot be opened.")
        val launched = launch(context, intent, "\"$target\" cannot be opened.")
        return if (launched.ok) Outcome.ok("package" to target) else launched
    }

    fun notify(context: Context, title: String, text: String, bigText: String?, tag: String?): Outcome {
        val manager = context.getSystemService(NotificationManager::class.java)
            ?: return Outcome.fail(Errors.UNAVAILABLE, "No notification service.")
        if (!Permissions.postNotifications(context)) {
            return Outcome.fail(Errors.PERMISSION_DENIED, "Notifications are turned off for Felix.")
        }
        Channels.ensure(context)
        val label = tag?.takeIf { it.isNotBlank() } ?: "felix"
        val body = bigText?.takeIf { it.isNotBlank() } ?: text
        val open = Intent(context, MainActivity::class.java)
            .setAction(MainActivity.ACTION_SHOW_TEXT)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra(MainActivity.EXTRA_TITLE, title)
            .putExtra(MainActivity.EXTRA_TEXT, body)
        val pending = PendingIntent.getActivity(
            context, label.hashCode(), open, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val notification = Notification.Builder(context, Channels.FELIX)
            .setSmallIcon(R.drawable.ic_stat_felix)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(Notification.BigTextStyle().bigText(body))
            .setContentIntent(pending)
            .setAutoCancel(true)
            .setShowWhen(true)
            .build()
        manager.notify(label, 1, notification)
        return Outcome.ok()
    }
}
