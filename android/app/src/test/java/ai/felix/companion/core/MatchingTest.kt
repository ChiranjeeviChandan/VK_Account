package ai.felix.companion.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class MatchingTest {
    @Test
    fun foldIgnoresCaseAccentsAndPunctuation() {
        assertEquals("jose alvarez", Matching.fold("  José  Álvarez! "))
        assertEquals("whatsapp", Matching.fold("WhatsApp"))
    }

    @Test
    fun scoresRankExactOverPrefixOverContains() {
        assertEquals(100, Matching.score("maps", "Maps"))
        assertEquals(90, Matching.score("whats app", "WhatsApp"))
        assertEquals(80, Matching.score("whats", "WhatsApp"))
        assertEquals(70, Matching.score("ravi k", "Ravi Shankar Kumar"))
        assertEquals(60, Matching.score("tube", "YouTube"))
        assertEquals(0, Matching.score("signal", "WhatsApp"))
        assertEquals(0, Matching.score("", "WhatsApp"))
    }

    @Test
    fun rankPicksTheBestAndReportsTies() {
        val apps = listOf("YouTube", "YouTube Music", "Maps", "Messages")
        assertEquals(0, Matching.rank("youtube", apps).best)
        assertFalse(Matching.rank("youtube", apps).ambiguous)
        assertEquals(2, Matching.rank("maps", apps).best)
        assertFalse(Matching.rank("spotify", apps).found)

        val people = listOf("Ravi Kumar", "Ravi Shah", "Priya", "Ravi Kumar")
        val ravi = Matching.rank("ravi", people)
        assertTrue(ravi.found)
        assertTrue(ravi.ambiguous)
        assertEquals(listOf(0, 1), ravi.tied)
        // Two rows for the same person are not a tie.
        assertFalse(Matching.rank("ravi kumar", people).ambiguous)
    }

    @Test
    fun numbers() {
        assertTrue(Matching.looksLikeNumber("+91 98123-45678"))
        assertTrue(Matching.looksLikeNumber("100"))
        assertFalse(Matching.looksLikeNumber("Ravi 2"))
        assertFalse(Matching.looksLikeNumber("12"))
        assertEquals("+919812345678", Matching.cleanNumber("+91 98123-45678"))
        assertEquals("08012345678", Matching.cleanNumber("(080) 1234 5678"))
    }
}
