package ai.felix.companion.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class JsonTest {
    @Test
    fun roundTripsNestedValues() {
        val value = linkedMapOf(
            "text" to "He said \"hi\"\n\ttab \\ slash / unicode \u00e9 \u4e2d",
            "int" to 42L,
            "neg" to -7L,
            "float" to 1.5,
            "yes" to true,
            "no" to false,
            "nothing" to null,
            "list" to listOf(1L, "two", listOf<Any?>(), mapOf("k" to "v")),
            "empty" to emptyMap<String, Any?>(),
        )
        assertEquals(value, Json.parse(Json.stringify(value)))
    }

    @Test
    fun parsesNumbersAsLongOrDouble() {
        val parsed = Json.parseObject("""{"a": 1794000000000, "b": 3.25, "c": -2, "d": 1e3, "e": 0}""")
        assertEquals(1794000000000L, parsed["a"])
        assertEquals(3.25, parsed["b"])
        assertEquals(-2L, parsed["c"])
        assertEquals(1000.0, parsed["d"])
        assertEquals(0L, parsed["e"])
        assertEquals(1000L, parsed.long("d"))
    }

    @Test
    fun parsesEscapes() {
        assertEquals("a\"b\\c/d\ne\u00e9", Json.parse(""""a\"b\\c\/d\ne\u00e9""""))
        assertEquals("\uD83D\uDE00", Json.parse(""""\ud83d\ude00""""))
    }

    @Test
    fun escapesControlCharacters() {
        assertEquals("\"\\u0001\\u001f\"", Json.stringify("\u0001\u001f"))
        assertEquals("\"line\\u2028sep\"", Json.stringify("line\u2028sep"))
    }

    @Test
    fun wholeDoublesAreWrittenAsIntegers() {
        assertEquals("[2,2.5,null]", Json.stringify(listOf(2.0, 2.5, Double.NaN)))
        assertEquals("{\"n\":7}", Json.stringify(mapOf("n" to 7)))
    }

    @Test
    fun rejectsMalformedInput() {
        for (bad in listOf("", "{", "[1,]", "{\"a\":}", "{'a':1}", "tru", "\"open", "1 2", "{\"a\":1,}", "[1 2]", "\"\\x\"", "1e999", "-", "1.2.3", "NaN")) {
            try {
                Json.parse(bad)
                fail("accepted malformed JSON: $bad")
            } catch (_: Json.ParseException) {
                // expected
            }
        }
    }

    @Test
    fun rejectsAbsurdNesting() {
        val deep = "[".repeat(500) + "]".repeat(500)
        try {
            Json.parse(deep)
            fail("accepted 500 levels of nesting")
        } catch (_: Json.ParseException) {
        }
    }

    @Test
    fun randomInputOnlyEverFailsWithParseException() {
        val random = java.util.Random(7)
        val alphabet = "{}[]\",:tfn0123456789.-+eE \\u\n\u00e9"
        repeat(5000) {
            val builder = StringBuilder()
            repeat(random.nextInt(30)) { builder.append(alphabet[random.nextInt(alphabet.length)]) }
            try {
                val value = Json.parse(builder.toString())
                // Whatever parses must be written back in a stable form.
                val written = Json.stringify(value)
                assertEquals(written, Json.stringify(Json.parse(written)))
            } catch (_: Json.ParseException) {
            }
        }
    }

    @Test
    fun parseObjectNeverThrows() {
        assertTrue(Json.parseObject(null).isEmpty())
        assertTrue(Json.parseObject("not json").isEmpty())
        assertTrue(Json.parseObject("[1,2]").isEmpty())
    }

    @Test
    fun accessorsToleratemistypedFields() {
        val json = Json.parseObject("""{"s": "x", "n": "12", "f": 3.9, "b": "yes", "o": {"k": 1}, "l": ["a", 2, null, {"z": 1}], "t": true}""")
        assertEquals("x", json.str("s"))
        assertEquals("true", json.str("t"))
        assertNull(json.str("missing"))
        assertNull(json.str("o"))
        assertEquals("d", json.strOr("missing", "d"))
        assertEquals(12L, json.long("n"))
        assertEquals(3, json.int("f"))
        assertNull(json.int("s"))
        assertTrue(json.bool("b"))
        assertFalse(json.bool("missing"))
        assertTrue(json.bool("missing", true))
        assertEquals(1L, json.obj("o")["k"])
        assertTrue(json.obj("s").isEmpty())
        assertEquals(listOf("a", "2"), json.strings("l"))
        assertTrue(json.list("missing").isEmpty())
    }
}
