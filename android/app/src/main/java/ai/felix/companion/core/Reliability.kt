package ai.felix.companion.core

/** Exponential backoff for the Core link, capped at one minute. */
class Backoff(
    private val baseMs: Long = 1_000L,
    private val maxMs: Long = 60_000L,
    private val jitter: (Long) -> Long = { it },
) {
    private var failures = 0

    val attempts: Int
        get() = failures

    fun reset() {
        failures = 0
    }

    /** Delay to wait after the failure that just happened. */
    fun nextDelayMs(): Long {
        val shift = failures.coerceAtMost(20)
        failures++
        val raw = if (shift >= 20) maxMs else baseMs * (1L shl shift)
        val capped = raw.coerceIn(baseMs, maxMs)
        return jitter(capped).coerceIn(0L, maxMs)
    }
}

/** A queued event as the trimming policy sees it. */
data class QueuedRef(val seq: Long, val type: String)

/**
 * Which queued events to give up when the phone has been away from Core for a
 * long time: status and plain notifications go first (oldest first), then
 * anything else, and messages last.
 */
object QueuePolicy {
    const val DEFAULT_MAX = 5_000

    fun rank(type: String): Int = when (type) {
        EventTypes.STATUS, EventTypes.NOTIFICATION_REMOVED -> 0
        EventTypes.NOTIFICATION -> 1
        EventTypes.MESSAGE, EventTypes.SMS -> 3
        else -> 2
    }

    /** Sequence numbers to delete so that at most [max] events remain. */
    fun toDrop(items: List<QueuedRef>, max: Int): List<Long> {
        val excess = items.size - max.coerceAtLeast(0)
        if (excess <= 0) return emptyList()
        return items
            .sortedWith(compareBy<QueuedRef> { rank(it.type) }.thenBy { it.seq })
            .take(excess)
            .map { it.seq }
    }
}

/** Remembers the most recent [capacity] keys; used to drop repeated notifications. */
class RecentSet(private val capacity: Int) {
    private val keys = LinkedHashSet<String>()

    /** Returns true when [key] was not seen before (and records it). */
    @Synchronized
    fun add(key: String): Boolean {
        if (!keys.add(key)) return false
        if (keys.size > capacity) {
            val oldest = keys.iterator()
            oldest.next()
            oldest.remove()
        }
        return true
    }

    @Synchronized
    fun contains(key: String): Boolean = key in keys

    @Synchronized
    fun remove(key: String): Boolean = keys.remove(key)

    @Synchronized
    fun size(): Int = keys.size
}

/**
 * Decides what to do with a delivered command given what the phone already
 * knows about its id. Commands are delivered at least once, and many of them
 * (sending an SMS, placing a call) must never run twice.
 */
object CommandLedger {
    const val STATE_RUNNING = "running"
    const val STATE_DONE = "done"

    sealed class Decision {
        /** First sighting: run it. */
        object Run : Decision()

        /** Already finished: answer with the stored result. */
        data class Replay(val resultJson: String) : Decision()

        /** It started earlier and never finished (the app died mid-way): do not run it again. */
        object Interrupted : Decision()
    }

    fun decide(state: String?, resultJson: String?): Decision = when {
        state == null -> Decision.Run
        state == STATE_DONE && !resultJson.isNullOrEmpty() -> Decision.Replay(resultJson)
        else -> Decision.Interrupted
    }
}

/** Incremental parser for `text/event-stream` responses. Feed it one line at a time. */
class SseParser(private val onEvent: (name: String, data: String) -> Unit) {
    private var name = ""
    private val data = StringBuilder()
    private var hasData = false

    fun feedLine(line: String) {
        if (line.isEmpty()) {
            dispatch()
            return
        }
        if (line.startsWith(":")) return // comment / keep-alive
        val colon = line.indexOf(':')
        val field = if (colon >= 0) line.substring(0, colon) else line
        var value = if (colon >= 0) line.substring(colon + 1) else ""
        if (value.startsWith(" ")) value = value.substring(1)
        when (field) {
            "event" -> name = value
            "data" -> {
                if (hasData) data.append('\n')
                data.append(value)
                hasData = true
            }
        }
    }

    /** Call at end of stream: flushes an event that was not followed by a blank line. */
    fun finish() = dispatch()

    private fun dispatch() {
        if (hasData || name.isNotEmpty()) {
            onEvent(name.ifEmpty { "message" }, data.toString())
        }
        name = ""
        data.setLength(0)
        hasData = false
    }
}
