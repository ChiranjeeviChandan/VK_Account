package ai.felix.companion.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReliabilityTest {
    @Test
    fun backoffDoublesAndCapsAtOneMinute() {
        val backoff = Backoff()
        val delays = (1..10).map { backoff.nextDelayMs() }
        assertEquals(listOf(1000L, 2000L, 4000L, 8000L, 16000L, 32000L, 60000L, 60000L, 60000L, 60000L), delays)
        assertEquals(10, backoff.attempts)
        backoff.reset()
        assertEquals(1000L, backoff.nextDelayMs())
    }

    @Test
    fun backoffNeverOverflowsOrExceedsTheCap() {
        val backoff = Backoff(jitter = { it * 3 })
        repeat(200) { assertTrue(backoff.nextDelayMs() in 0L..60_000L) }
    }

    @Test
    fun queueKeepsEverythingUnderTheLimit() {
        val items = (1L..10L).map { QueuedRef(it, "notification") }
        assertTrue(QueuePolicy.toDrop(items, 10).isEmpty())
        assertTrue(QueuePolicy.toDrop(items, 50).isEmpty())
    }

    @Test
    fun queueDropsStatusThenNotificationsThenOthersAndMessagesLast() {
        val items = listOf(
            QueuedRef(1, "message"),
            QueuedRef(2, "notification"),
            QueuedRef(3, "status"),
            QueuedRef(4, "call"),
            QueuedRef(5, "notification"),
            QueuedRef(6, "sms"),
            QueuedRef(7, "notification_removed"),
            QueuedRef(8, "message"),
        )
        assertEquals(listOf(3L, 7L), QueuePolicy.toDrop(items, 6))
        assertEquals(listOf(3L, 7L, 2L, 5L), QueuePolicy.toDrop(items, 4))
        assertEquals(listOf(3L, 7L, 2L, 5L, 4L), QueuePolicy.toDrop(items, 3))
        // Only when nothing else is left do messages go, oldest first.
        assertEquals(listOf(3L, 7L, 2L, 5L, 4L, 1L), QueuePolicy.toDrop(items, 2))
        assertEquals(8, QueuePolicy.toDrop(items, 0).size)
    }

    @Test
    fun recentSetEvictsOldestKeys() {
        val set = RecentSet(3)
        assertTrue(set.add("a"))
        assertFalse(set.add("a"))
        assertTrue(set.add("b"))
        assertTrue(set.add("c"))
        assertTrue(set.add("d"))
        assertEquals(3, set.size())
        assertFalse(set.contains("a"))
        assertTrue(set.contains("d"))
        assertTrue(set.add("a"))
        assertTrue(set.remove("a"))
        assertFalse(set.remove("a"))
    }

    @Test
    fun ledgerRunsOnceReplaysAndRefusesInterruptedCommands() {
        assertEquals(CommandLedger.Decision.Run, CommandLedger.decide(null, null))
        assertEquals(
            CommandLedger.Decision.Replay("""{"id":1,"ok":true}"""),
            CommandLedger.decide(CommandLedger.STATE_DONE, """{"id":1,"ok":true}""")
        )
        assertEquals(CommandLedger.Decision.Interrupted, CommandLedger.decide(CommandLedger.STATE_RUNNING, null))
        assertEquals(CommandLedger.Decision.Interrupted, CommandLedger.decide(CommandLedger.STATE_DONE, ""))
    }

    @Test
    fun sseParserFollowsTheStreamFormat() {
        val events = ArrayList<Pair<String, String>>()
        val parser = SseParser { name, data -> events.add(name to data) }
        val stream = listOf(
            ": keep-alive",
            "event: delta",
            "data: {\"text\": \"Hel\"}",
            "",
            "event: delta",
            "data:{\"text\": \"lo\"}",
            "",
            "data: line one",
            "data: line two",
            "",
            "",
            "event: done",
            "data: {\"reply\": \"Hello\"}",
        )
        stream.forEach(parser::feedLine)
        assertEquals(3, events.size)
        parser.finish()
        assertEquals(
            listOf(
                "delta" to "{\"text\": \"Hel\"}",
                "delta" to "{\"text\": \"lo\"}",
                "message" to "line one\nline two",
                "done" to "{\"reply\": \"Hello\"}",
            ),
            events
        )
        parser.finish()
        assertEquals(4, events.size)
    }
}
