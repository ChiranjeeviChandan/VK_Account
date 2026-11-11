package ai.felix.companion.link

import ai.felix.companion.Db
import ai.felix.companion.Hub
import ai.felix.companion.Prefs
import ai.felix.companion.R
import ai.felix.companion.core.Backoff
import ai.felix.companion.core.Command
import ai.felix.companion.core.CommandLedger
import ai.felix.companion.core.CommandResult
import ai.felix.companion.core.Errors
import ai.felix.companion.core.EventTypes
import ai.felix.companion.core.Json
import ai.felix.companion.core.Outcome
import ai.felix.companion.core.Wire
import ai.felix.companion.device.Channels
import ai.felix.companion.device.ContactsBridge
import ai.felix.companion.device.Controls
import ai.felix.companion.device.DeviceStatus
import ai.felix.companion.device.Locator
import ai.felix.companion.device.Permissions
import ai.felix.companion.net.CoreClient
import ai.felix.companion.notify.FelixNotificationListener
import ai.felix.companion.overlay.OverlayController
import ai.felix.companion.ui.MainActivity
import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.SystemClock
import android.service.notification.NotificationListenerService
import android.telephony.TelephonyManager
import java.io.IOException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread
import kotlin.random.Random

/**
 * The always-on half of the companion. It keeps the long poll to Felix Core
 * open, runs the commands that arrive, uploads queued events and results, and
 * hosts the floating overlay.
 */
class FelixLinkService : Service() {
    private lateinit var prefs: Prefs
    private lateinit var db: Db
    private lateinit var client: CoreClient
    private lateinit var executor: CommandExecutor

    @Volatile
    private var running = false
    private var pollThread: Thread? = null
    private var uploadThread: Thread? = null

    /** Released to cut a reconnect pause short without disturbing a request that is in flight. */
    private val retryNow = Semaphore(0)
    private val commandRunner: ExecutorService = Executors.newSingleThreadExecutor { Thread(it, "felix-commands") }
    private val background: ExecutorService = Executors.newSingleThreadExecutor { Thread(it, "felix-aux") }

    private var overlay: OverlayController? = null
    private var phoneReceiver: BroadcastReceiver? = null
    private var shownStatus = ""
    private var shownRinging = false
    private var lastStatusJson = ""
    private var lastStatusAt = 0L
    private var lastPruneAt = 0L

    private val stateListener: () -> Unit = { refreshNotification() }

    override fun onCreate() {
        super.onCreate()
        prefs = Hub.prefs(this)
        db = Hub.db(this)
        client = CoreClient(prefs)
        executor = CommandExecutor(this)
        Channels.ensure(this)
        if (!enterForeground()) {
            stopSelf()
            return
        }
        running = true
        isRunning = true
        pollThread = thread(name = "felix-poll", isDaemon = true) { pollLoop() }
        uploadThread = thread(name = "felix-upload", isDaemon = true) { uploadLoop() }
        registerPhoneState()
        Hub.addListener(stateListener)
        if (Permissions.notificationListener(this) && FelixNotificationListener.instance == null) {
            try {
                NotificationListenerService.requestRebind(ComponentName(this, FelixNotificationListener::class.java))
            } catch (_: RuntimeException) {
                // Android rebinds the listener on its own schedule.
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!running) {
            // onCreate could not enter the foreground; nothing to do.
            stopSelf()
            return START_NOT_STICKY
        }
        enterForeground()
        when (intent?.action) {
            ACTION_STOP_LOCATE -> {
                Locator.stop(this)
                refreshNotification(force = true)
            }
            ACTION_REFRESH -> {
                retryNow.release()
                Hub.nudge()
            }
        }
        syncOverlay()
        if (!prefs.isPaired && !prefs.overlayEnabled) {
            stopSelf()
            return START_NOT_STICKY
        }
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        running = false
        isRunning = false
        Hub.removeListener(stateListener)
        pollThread?.interrupt()
        uploadThread?.interrupt()
        retryNow.release(2)
        Hub.nudge()
        commandRunner.shutdownNow()
        background.shutdownNow()
        overlay?.destroy()
        overlay = null
        phoneReceiver?.let {
            try {
                unregisterReceiver(it)
            } catch (_: IllegalArgumentException) {
            }
        }
        phoneReceiver = null
        Locator.stop(this)
        Hub.updateLink(false)
        super.onDestroy()
    }

    // ---- foreground notification ---------------------------------------

    private fun statusText(): String = when {
        !prefs.isPaired -> getString(R.string.status_not_paired)
        prefs.revoked -> getString(R.string.status_revoked)
        Hub.linkState.connected -> getString(R.string.status_connected, prefs.coreName)
        else -> getString(R.string.status_searching, prefs.coreName)
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        val builder = Notification.Builder(this, Channels.LINK)
            .setSmallIcon(R.drawable.ic_stat_felix)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .setShowWhen(false)
        if (Locator.isRinging) {
            val stop = PendingIntent.getService(
                this, 1,
                Intent(this, FelixLinkService::class.java).setAction(ACTION_STOP_LOCATE),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            builder.addAction(Notification.Action.Builder(null, getString(R.string.action_stop_ringing), stop).build())
        }
        return builder.build()
    }

    private fun enterForeground(): Boolean {
        val text = statusText()
        shownStatus = text
        shownRinging = Locator.isRinging
        return try {
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(NOTIFICATION_ID, buildNotification(text), ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
            } else {
                startForeground(NOTIFICATION_ID, buildNotification(text))
            }
            true
        } catch (_: RuntimeException) {
            // Android refused a background start (battery restrictions). The next
            // user interaction with the app starts the service again.
            false
        }
    }

    private fun refreshNotification(force: Boolean = false) {
        if (!running) return
        val text = statusText()
        val ringing = Locator.isRinging
        if (!force && text == shownStatus && ringing == shownRinging) return
        shownStatus = text
        shownRinging = ringing
        try {
            getSystemService(NotificationManager::class.java)?.notify(NOTIFICATION_ID, buildNotification(text))
        } catch (_: RuntimeException) {
        }
    }

    // ---- overlay --------------------------------------------------------

    private fun syncOverlay() {
        val wanted = prefs.overlayEnabled && Permissions.overlay(this)
        if (wanted && overlay == null) {
            overlay = OverlayController(this, client, executor).also { it.show() }
        } else if (!wanted && overlay != null) {
            overlay?.destroy()
            overlay = null
        }
    }

    // ---- commands from Core --------------------------------------------

    private fun pollLoop() {
        val backoff = Backoff(jitter = { it + Random.nextLong(0, 500) })
        while (running) {
            if (!prefs.canSync) {
                Hub.updateLink(false, if (prefs.revoked) getString(R.string.status_revoked) else null)
                sleepQuietly(3_000)
                continue
            }
            try {
                val wait = prefs.pollWait
                val result = client.request("GET", "/api/phone/poll?wait=$wait", null, (wait + 15) * 1000)
                when {
                    result.code == 401 -> onRevoked()
                    !result.ok -> throw IOException("Felix Core answered with HTTP ${result.code}.")
                    else -> {
                        backoff.reset()
                        Hub.updateLink(true)
                        for (command in Wire.parsePoll(result.body)) submit(command)
                    }
                }
            } catch (e: IOException) {
                if (!running) break
                Hub.updateLink(false, e.message)
                sleepQuietly(backoff.nextDelayMs())
            } catch (e: RuntimeException) {
                if (!running) break
                Hub.updateLink(false, e.message)
                sleepQuietly(backoff.nextDelayMs())
            }
        }
    }

    private fun submit(command: Command) {
        try {
            commandRunner.execute { runCommand(command) }
        } catch (_: RejectedExecutionException) {
            // Shutting down; Core delivers the command again later.
        }
    }

    private fun runCommand(command: Command) {
        val now = System.currentTimeMillis()
        try {
            when (db.beginCommand(command.id, now)) {
                is CommandLedger.Decision.Replay -> Unit // the stored result is queued for upload again
                is CommandLedger.Decision.Interrupted -> store(
                    command.id,
                    Outcome.fail(Errors.FAILED, "The phone restarted while this command was running, so it was not run a second time.")
                )
                is CommandLedger.Decision.Run -> store(command.id, executor.execute(command, now))
            }
        } catch (_: RuntimeException) {
            // The ledger could not be written (storage failure). Core delivers the command again.
        }
        Hub.nudge()
    }

    private fun store(id: Long, outcome: Outcome) {
        db.finishCommand(id, Json.stringify(CommandResult.of(id, outcome).toJson()), System.currentTimeMillis())
    }

    // ---- uploads to Core -----------------------------------------------

    private fun uploadLoop() {
        val backoff = Backoff(jitter = { it + Random.nextLong(0, 500) })
        while (running) {
            Hub.awaitWork(UPLOAD_IDLE_MS)
            if (!running) break
            if (!prefs.canSync) continue
            try {
                queueStatusIfDue()
                flushResults()
                flushEvents()
                backoff.reset()
                pruneOccasionally()
            } catch (e: IOException) {
                if (!running) break
                sleepQuietly(backoff.nextDelayMs())
                Hub.nudge() // retry straight after the pause instead of waiting for new work
            } catch (e: RuntimeException) {
                if (!running) break
                sleepQuietly(backoff.nextDelayMs())
            }
        }
    }

    private fun queueStatusIfDue() {
        val now = SystemClock.elapsedRealtime()
        val status = DeviceStatus.collect(this)
        val json = Json.stringify(status)
        val changed = json != lastStatusJson && now - lastStatusAt > STATUS_MIN_GAP_MS
        if (lastStatusAt == 0L || changed || now - lastStatusAt > STATUS_HEARTBEAT_MS) {
            lastStatusJson = json
            lastStatusAt = now
            Hub.emit(this, EventTypes.STATUS, status)
        }
    }

    @Throws(IOException::class)
    private fun flushResults() {
        while (running) {
            val pending = db.pendingResults(50)
            if (pending.isEmpty()) return
            // Stored results are complete JSON objects already.
            val body = pending.joinToString(separator = ",", prefix = "{\"results\":[", postfix = "]}") { it.second }
            val response = client.request("POST", "/api/phone/results", body)
            if (response.code == 401) {
                onRevoked()
                return
            }
            if (!response.ok) throw IOException("Felix Core refused results with HTTP ${response.code}.")
            db.markResultsDelivered(pending.map { it.first })
        }
    }

    @Throws(IOException::class)
    private fun flushEvents() {
        while (running) {
            val batch = db.peekEvents(Wire.MAX_EVENTS_PER_REQUEST)
            if (batch.isEmpty()) return
            val response = client.request("POST", "/api/phone/events", Wire.eventsBody(batch))
            if (response.code == 401) {
                onRevoked()
                return
            }
            if (!response.ok) throw IOException("Felix Core refused events with HTTP ${response.code}.")
            Hub.updateLink(true)
            val accepted = Wire.parseAccepted(response.body)
            db.removeEvents(accepted)
            val declined = batch.map { it.id }.filter { it !in accepted }
            if (declined.isNotEmpty()) {
                db.noteRejected(declined)
                if (accepted.isEmpty()) return // do not spin on events Core will not take
            }
        }
    }

    private fun pruneOccasionally() {
        val now = SystemClock.elapsedRealtime()
        if (now - lastPruneAt < 6 * 3600 * 1000L && lastPruneAt != 0L) return
        lastPruneAt = now
        db.pruneCommands(System.currentTimeMillis())
    }

    private fun onRevoked() {
        if (prefs.revoked) return
        prefs.revoked = true
        Hub.updateLink(false, getString(R.string.status_revoked))
        Controls.notify(this, getString(R.string.app_name), getString(R.string.status_revoked), null, "felix-pairing")
    }

    /** Pauses for [ms], or less when a refresh asks for an immediate retry or the service stops. */
    private fun sleepQuietly(ms: Long) {
        try {
            if (retryNow.tryAcquire(ms, TimeUnit.MILLISECONDS)) retryNow.drainPermits()
        } catch (_: InterruptedException) {
            // The service is shutting down.
        }
    }

    // ---- phone calls ----------------------------------------------------

    private fun registerPhoneState() {
        val receiver = object : BroadcastReceiver() {
            private var previous = TelephonyManager.EXTRA_STATE_IDLE
            private var number: String? = null
            private var ringReported = false

            override fun onReceive(context: Context, intent: Intent) {
                if (intent.action != TelephonyManager.ACTION_PHONE_STATE_CHANGED) return
                val state = intent.getStringExtra(TelephonyManager.EXTRA_STATE) ?: return
                @Suppress("DEPRECATION")
                val incoming = intent.getStringExtra(TelephonyManager.EXTRA_INCOMING_NUMBER)
                if (!incoming.isNullOrEmpty()) number = incoming
                when (state) {
                    TelephonyManager.EXTRA_STATE_RINGING -> {
                        // With call-log access Android sends this twice; the second copy carries the number.
                        val numberComing = Permissions.has(context, android.Manifest.permission.READ_CALL_LOG)
                        if (!ringReported && (number != null || !numberComing)) {
                            ringReported = true
                            report("ringing", number)
                        }
                    }
                    TelephonyManager.EXTRA_STATE_OFFHOOK ->
                        if (previous == TelephonyManager.EXTRA_STATE_RINGING) report("answered", number)
                    TelephonyManager.EXTRA_STATE_IDLE -> {
                        if (previous == TelephonyManager.EXTRA_STATE_RINGING) {
                            if (!ringReported) report("ringing", number)
                            report("missed", number)
                        } else if (previous == TelephonyManager.EXTRA_STATE_OFFHOOK) {
                            report("ended", number)
                        }
                        number = null
                        ringReported = false
                    }
                }
                previous = state
            }
        }
        val filter = IntentFilter(TelephonyManager.ACTION_PHONE_STATE_CHANGED)
        try {
            if (Build.VERSION.SDK_INT >= 33) {
                registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED)
            } else {
                registerReceiver(receiver, filter)
            }
            phoneReceiver = receiver
        } catch (_: RuntimeException) {
            phoneReceiver = null
        }
    }

    private fun report(state: String, number: String?) {
        val app = applicationContext
        try {
            background.execute {
                var name = ""
                if (!number.isNullOrEmpty() && Permissions.contacts(app)) {
                    name = try {
                        ContactsBridge.find(app, number, 1).firstOrNull()?.name.orEmpty()
                    } catch (_: RuntimeException) {
                        ""
                    }
                }
                Hub.emit(app, EventTypes.CALL, linkedMapOf("state" to state, "number" to (number ?: ""), "name" to name))
            }
        } catch (_: RejectedExecutionException) {
        }
    }

    companion object {
        const val ACTION_REFRESH = "ai.felix.companion.action.REFRESH"
        const val ACTION_STOP_LOCATE = "ai.felix.companion.action.STOP_LOCATE"

        private const val NOTIFICATION_ID = 41
        private const val UPLOAD_IDLE_MS = 30_000L
        private const val STATUS_MIN_GAP_MS = 30_000L
        private const val STATUS_HEARTBEAT_MS = 5 * 60_000L

        @Volatile
        var isRunning = false
            private set

        /** Starts the service when there is something for it to do (a pairing or the overlay). */
        fun startIfNeeded(context: Context) {
            if (isRunning) return
            val prefs = Hub.prefs(context)
            if (prefs.isPaired || prefs.overlayEnabled) start(context, null)
        }

        /** Tells a running service that pairing or settings changed (and starts it if it should run). */
        fun refresh(context: Context) {
            val prefs = Hub.prefs(context)
            if (isRunning || prefs.isPaired || prefs.overlayEnabled) start(context, ACTION_REFRESH)
        }

        private fun start(context: Context, action: String?) {
            val intent = Intent(context.applicationContext, FelixLinkService::class.java)
            if (action != null) intent.action = action
            try {
                context.applicationContext.startForegroundService(intent)
            } catch (_: IllegalStateException) {
                // Not allowed from the background right now; opening the app starts it.
            } catch (_: SecurityException) {
            }
        }
    }
}
