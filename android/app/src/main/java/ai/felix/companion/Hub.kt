package ai.felix.companion

import ai.felix.companion.core.Event
import android.content.Context
import android.os.Handler
import android.os.Looper
import java.util.UUID
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock

/** State of the connection to Felix Core, for the status card and the overlay. */
data class LinkState(
    val connected: Boolean = false,
    val lastContactMs: Long = 0L,
    val lastError: String? = null,
    val coreOnline: Boolean? = null,
)

/**
 * Process-wide meeting point for the services: the notification listener, the
 * SMS receiver and the accessibility service drop events here, and the link
 * service picks them up.
 */
object Hub {
    private val mainHandler = Handler(Looper.getMainLooper())
    private val listeners = CopyOnWriteArraySet<() -> Unit>()
    private val workLock = ReentrantLock()
    private val workAvailable = workLock.newCondition()

    @Volatile
    private var database: Db? = null

    @Volatile
    private var preferences: Prefs? = null

    @Volatile
    var linkState: LinkState = LinkState()
        private set

    /** Increases whenever something is waiting to be uploaded. */
    @Volatile
    private var pending = 0L

    fun db(context: Context): Db {
        database?.let { return it }
        synchronized(this) {
            database?.let { return it }
            val created = Db(context.applicationContext)
            database = created
            return created
        }
    }

    fun prefs(context: Context): Prefs {
        preferences?.let { return it }
        synchronized(this) {
            preferences?.let { return it }
            val created = Prefs(context.applicationContext)
            preferences = created
            return created
        }
    }

    /** Queues an event for Core and wakes the uploader. Safe to call from any thread. */
    fun emit(context: Context, type: String, data: Map<String, Any?>, id: String = UUID.randomUUID().toString()) {
        val added = try {
            db(context).enqueue(Event(id, type, System.currentTimeMillis(), data))
        } catch (_: RuntimeException) {
            false // storage is full or failing: losing one event beats crashing a system callback
        }
        if (added) nudge()
    }

    /** Wakes the uploader thread of the link service, if it is running. */
    fun nudge() {
        workLock.withLock {
            pending++
            workAvailable.signalAll()
        }
    }

    /** Blocks until [nudge] is called or [timeoutMs] passes. */
    fun awaitWork(timeoutMs: Long) {
        workLock.withLock {
            if (pending == 0L) {
                try {
                    workAvailable.await(timeoutMs, TimeUnit.MILLISECONDS)
                } catch (_: InterruptedException) {
                    Thread.currentThread().interrupt()
                }
            }
            pending = 0
        }
    }

    fun setLinkState(state: LinkState) {
        if (state == linkState) return
        linkState = state
        notifyListeners()
    }

    fun updateLink(connected: Boolean, error: String? = null) {
        val previous = linkState
        setLinkState(
            previous.copy(
                connected = connected,
                lastContactMs = if (connected) System.currentTimeMillis() else previous.lastContactMs,
                lastError = if (connected) null else (error ?: previous.lastError),
            )
        )
    }

    /** Listeners are always called on the main thread. */
    fun addListener(listener: () -> Unit) {
        listeners.add(listener)
    }

    fun removeListener(listener: () -> Unit) {
        listeners.remove(listener)
    }

    fun notifyListeners() {
        mainHandler.post {
            for (listener in listeners) {
                try {
                    listener()
                } catch (_: RuntimeException) {
                    // A broken screen must not take the services down with it.
                }
            }
        }
    }

    fun runOnMain(block: () -> Unit) {
        if (Looper.myLooper() == Looper.getMainLooper()) block() else mainHandler.post(block)
    }

    fun postDelayed(delayMs: Long, block: () -> Unit) {
        mainHandler.postDelayed(block, delayMs)
    }
}
