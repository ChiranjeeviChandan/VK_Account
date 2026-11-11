package ai.felix.companion.core

/**
 * A small, dependency-free JSON codec.
 *
 * Values are plain Kotlin types: `Map<String, Any?>`, `List<Any?>`, `String`,
 * `Long` (integers), `Double` (everything else numeric), `Boolean` and `null`.
 * Keeping this free of Android classes lets the whole protocol layer run in
 * ordinary JVM unit tests.
 */
object Json {
    class ParseException(message: String) : Exception(message)

    fun parse(text: String): Any? {
        val parser = Parser(text)
        parser.skipWhitespace()
        val value = parser.readValue(0)
        parser.skipWhitespace()
        if (!parser.atEnd()) throw ParseException("Unexpected data at position ${parser.pos}")
        return value
    }

    /** Parses [text] and returns it when it is a JSON object, otherwise an empty map. */
    fun parseObject(text: String?): Map<String, Any?> {
        if (text.isNullOrBlank()) return emptyMap()
        return try {
            asObject(parse(text))
        } catch (_: ParseException) {
            emptyMap()
        }
    }

    fun asObject(value: Any?): Map<String, Any?> {
        if (value !is Map<*, *>) return emptyMap()
        val out = LinkedHashMap<String, Any?>(value.size)
        for ((key, item) in value) {
            if (key is String) out[key] = item
        }
        return out
    }

    fun asList(value: Any?): List<Any?> = if (value is List<*>) value else emptyList()

    fun stringify(value: Any?): String {
        val out = StringBuilder()
        write(out, value)
        return out.toString()
    }

    private fun write(out: StringBuilder, value: Any?) {
        when (value) {
            null -> out.append("null")
            is String -> writeString(out, value)
            is Boolean -> out.append(if (value) "true" else "false")
            is Int, is Long, is Short, is Byte -> out.append(value.toString())
            is Float -> writeDouble(out, value.toDouble())
            is Double -> writeDouble(out, value)
            is Number -> writeDouble(out, value.toDouble())
            is Map<*, *> -> {
                out.append('{')
                var first = true
                for ((key, item) in value) {
                    if (!first) out.append(',')
                    first = false
                    writeString(out, key.toString())
                    out.append(':')
                    write(out, item)
                }
                out.append('}')
            }
            is Iterable<*> -> {
                out.append('[')
                var first = true
                for (item in value) {
                    if (!first) out.append(',')
                    first = false
                    write(out, item)
                }
                out.append(']')
            }
            is Array<*> -> write(out, value.asList())
            is CharSequence -> writeString(out, value.toString())
            else -> writeString(out, value.toString())
        }
    }

    private fun writeDouble(out: StringBuilder, value: Double) {
        if (value.isNaN() || value.isInfinite()) {
            out.append("null") // JSON has no representation for these
        } else if (value == Math.rint(value) && Math.abs(value) < 1e15) {
            out.append(value.toLong().toString())
        } else {
            out.append(value.toString())
        }
    }

    private fun writeString(out: StringBuilder, text: String) {
        out.append('"')
        for (ch in text) {
            when (ch) {
                '"' -> out.append("\\\"")
                '\\' -> out.append("\\\\")
                '\n' -> out.append("\\n")
                '\r' -> out.append("\\r")
                '\t' -> out.append("\\t")
                '\b' -> out.append("\\b")
                '\u000C' -> out.append("\\f")
                else -> if (ch < ' ' || ch == ' ' || ch == ' ') {
                    out.append("\\u")
                    val hex = Integer.toHexString(ch.code)
                    for (i in hex.length until 4) out.append('0')
                    out.append(hex)
                } else {
                    out.append(ch)
                }
            }
        }
        out.append('"')
    }

    private class Parser(private val text: String) {
        var pos = 0

        fun atEnd(): Boolean = pos >= text.length

        fun skipWhitespace() {
            while (pos < text.length) {
                val ch = text[pos]
                if (ch == ' ' || ch == '\n' || ch == '\r' || ch == '\t') pos++ else break
            }
        }

        fun readValue(depth: Int): Any? {
            if (depth > MAX_DEPTH) throw ParseException("JSON is nested too deeply")
            if (atEnd()) throw ParseException("Unexpected end of JSON")
            return when (val ch = text[pos]) {
                '{' -> readObject(depth)
                '[' -> readArray(depth)
                '"' -> readString()
                't' -> readLiteral("true", true)
                'f' -> readLiteral("false", false)
                'n' -> readLiteral("null", null)
                else -> if (ch == '-' || (ch in '0'..'9')) readNumber() else throw ParseException("Unexpected '$ch' at position $pos")
            }
        }

        private fun readLiteral(word: String, value: Any?): Any? {
            if (!text.startsWith(word, pos)) throw ParseException("Unexpected token at position $pos")
            pos += word.length
            return value
        }

        private fun readObject(depth: Int): Map<String, Any?> {
            val out = LinkedHashMap<String, Any?>()
            pos++ // {
            skipWhitespace()
            if (!atEnd() && text[pos] == '}') {
                pos++
                return out
            }
            while (true) {
                skipWhitespace()
                if (atEnd() || text[pos] != '"') throw ParseException("Expected a key at position $pos")
                val key = readString()
                skipWhitespace()
                if (atEnd() || text[pos] != ':') throw ParseException("Expected ':' at position $pos")
                pos++
                skipWhitespace()
                out[key] = readValue(depth + 1)
                skipWhitespace()
                if (atEnd()) throw ParseException("Unterminated object")
                when (text[pos]) {
                    ',' -> pos++
                    '}' -> {
                        pos++
                        return out
                    }
                    else -> throw ParseException("Expected ',' or '}' at position $pos")
                }
            }
        }

        private fun readArray(depth: Int): List<Any?> {
            val out = ArrayList<Any?>()
            pos++ // [
            skipWhitespace()
            if (!atEnd() && text[pos] == ']') {
                pos++
                return out
            }
            while (true) {
                skipWhitespace()
                out.add(readValue(depth + 1))
                skipWhitespace()
                if (atEnd()) throw ParseException("Unterminated array")
                when (text[pos]) {
                    ',' -> pos++
                    ']' -> {
                        pos++
                        return out
                    }
                    else -> throw ParseException("Expected ',' or ']' at position $pos")
                }
            }
        }

        private fun readString(): String {
            pos++ // opening quote
            val out = StringBuilder()
            while (true) {
                if (atEnd()) throw ParseException("Unterminated string")
                val ch = text[pos++]
                when (ch) {
                    '"' -> return out.toString()
                    '\\' -> {
                        if (atEnd()) throw ParseException("Unterminated escape")
                        when (val esc = text[pos++]) {
                            '"' -> out.append('"')
                            '\\' -> out.append('\\')
                            '/' -> out.append('/')
                            'b' -> out.append('\b')
                            'f' -> out.append('\u000C')
                            'n' -> out.append('\n')
                            'r' -> out.append('\r')
                            't' -> out.append('\t')
                            'u' -> {
                                if (pos + 4 > text.length) throw ParseException("Bad unicode escape")
                                val code = text.substring(pos, pos + 4).toIntOrNull(16)
                                    ?: throw ParseException("Bad unicode escape at position $pos")
                                out.append(code.toChar())
                                pos += 4
                            }
                            else -> throw ParseException("Bad escape '\\$esc' at position ${pos - 1}")
                        }
                    }
                    else -> out.append(ch)
                }
            }
        }

        private fun readNumber(): Any {
            val start = pos
            if (text[pos] == '-') pos++
            var integral = true
            while (pos < text.length) {
                val ch = text[pos]
                if (ch in '0'..'9') {
                    pos++
                } else if (ch == '.' || ch == 'e' || ch == 'E' || ch == '+' || ch == '-') {
                    integral = false
                    pos++
                } else {
                    break
                }
            }
            val token = text.substring(start, pos)
            if (integral) {
                token.toLongOrNull()?.let { return it }
            }
            val number = token.toDoubleOrNull()
            if (number == null || number.isNaN() || number.isInfinite()) {
                throw ParseException("Bad number '$token' at position $start")
            }
            return number
        }
    }

    private const val MAX_DEPTH = 64
}

// Typed accessors that never throw: a missing or mistyped field yields the default.

fun Map<String, Any?>.str(key: String): String? = when (val value = this[key]) {
    null -> null
    is String -> value
    is Number, is Boolean -> value.toString()
    else -> null
}

fun Map<String, Any?>.strOr(key: String, default: String): String = str(key) ?: default

fun Map<String, Any?>.long(key: String): Long? = when (val value = this[key]) {
    is Long -> value
    is Int -> value.toLong()
    is Number -> value.toDouble().let { if (it.isNaN() || it.isInfinite()) null else it.toLong() }
    is String -> value.trim().toLongOrNull() ?: value.trim().toDoubleOrNull()?.toLong()
    else -> null
}

fun Map<String, Any?>.int(key: String): Int? {
    val value = long(key) ?: return null
    return if (value in Int.MIN_VALUE.toLong()..Int.MAX_VALUE.toLong()) value.toInt() else null
}

fun Map<String, Any?>.double(key: String): Double? = when (val value = this[key]) {
    is Number -> value.toDouble()
    is String -> value.trim().toDoubleOrNull()
    else -> null
}

fun Map<String, Any?>.bool(key: String, default: Boolean = false): Boolean = when (val value = this[key]) {
    is Boolean -> value
    is Number -> value.toDouble() != 0.0
    is String -> when (value.trim().lowercase()) {
        "true", "1", "yes", "on" -> true
        "false", "0", "no", "off" -> false
        else -> default
    }
    else -> default
}

fun Map<String, Any?>.obj(key: String): Map<String, Any?> = Json.asObject(this[key])

fun Map<String, Any?>.list(key: String): List<Any?> = Json.asList(this[key])

fun Map<String, Any?>.strings(key: String): List<String> = list(key).mapNotNull {
    when (it) {
        is String -> it
        is Number -> it.toString()
        else -> null
    }
}
