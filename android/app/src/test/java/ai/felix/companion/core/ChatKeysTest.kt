package ai.felix.companion.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ChatKeysTest {
    @Test
    fun channelsFollowTheProtocolNames() {
        assertEquals("whatsapp", ChatKeys.channelFor("com.whatsapp"))
        assertEquals("whatsapp_business", ChatKeys.channelFor("com.whatsapp.w4b"))
        assertEquals("sms", ChatKeys.channelFor("com.google.android.apps.messaging"))
        assertEquals("sms", ChatKeys.channelFor("com.samsung.android.messaging"))
        assertEquals("telegram", ChatKeys.channelFor("org.telegram.messenger"))
        assertEquals("teams", ChatKeys.channelFor("com.microsoft.teams"))
        assertEquals("slack", ChatKeys.channelFor("com.Slack"))
        assertEquals("signal", ChatKeys.channelFor("org.thoughtcrime.securesms"))
        assertEquals("other", ChatKeys.channelFor("com.example.app"))
        assertTrue(ChatKeys.isMessagingPackage("com.whatsapp"))
        assertFalse(ChatKeys.isMessagingPackage("com.google.android.gm"))
    }

    @Test
    fun chatKeyPrefersShortcutThenTagThenTitle() {
        assertEquals(
            "com.whatsapp|919812345678@s.whatsapp.net",
            ChatKeys.chatKey("com.whatsapp", "919812345678@s.whatsapp.net", "tag", "Ravi")
        )
        assertEquals("com.whatsapp|tag-1", ChatKeys.chatKey("com.whatsapp", "  ", "tag-1", "Ravi"))
        assertEquals("com.whatsapp|Ravi Kumar", ChatKeys.chatKey("com.whatsapp", null, null, "Ravi Kumar (2 messages)"))
        assertEquals("com.whatsapp|unknown", ChatKeys.chatKey("com.whatsapp", null, "", null))
    }

    @Test
    fun titleCleaning() {
        assertEquals("Board Group", ChatKeys.cleanTitle("Board Group (12 new messages)"))
        assertEquals("Ravi", ChatKeys.cleanTitle("Ravi (1 message)"))
        assertEquals("Plan (final)", ChatKeys.cleanTitle("Plan (final)"))
        assertEquals("Board Group" to "Priya", ChatKeys.splitGroupTitle("Board Group: Priya"))
        assertNull(ChatKeys.splitGroupTitle("Ravi Kumar"))
    }

    @Test
    fun messageEventIdsAreStableAndDistinct() {
        val a = ChatKeys.messageEventId("com.whatsapp|x", "Ravi", "hello", 1794000000000L)
        val again = ChatKeys.messageEventId("com.whatsapp|x", "Ravi", "hello", 1794000000000L)
        val later = ChatKeys.messageEventId("com.whatsapp|x", "Ravi", "hello", 1794000000001L)
        val other = ChatKeys.messageEventId("com.whatsapp|x", "Rav", "ihello", 1794000000000L)
        assertEquals(a, again)
        assertNotEquals(a, later)
        assertNotEquals(a, other)
        assertEquals(36, a.length)
    }
}
