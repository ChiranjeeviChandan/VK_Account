package ai.felix.companion.core

import java.net.URLDecoder
import java.security.MessageDigest

/** Everything the phone needs to reach and trust one Felix Core. */
data class PairingInfo(
    val version: Int,
    val scheme: String,
    val hosts: List<String>,
    val port: Int,
    val code: String,
    val fingerprint: String?,
    val name: String,
)

object Fingerprints {
    private const val HEX = "0123456789abcdef"

    /** SHA-256 of [bytes] as lower-case hex with no separators. */
    fun sha256Hex(bytes: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(bytes)
        val out = StringBuilder(digest.size * 2)
        for (b in digest) {
            val v = b.toInt() and 0xff
            out.append(HEX[v ushr 4]).append(HEX[v and 0x0f])
        }
        return out.toString()
    }

    /**
     * Accepts a fingerprint the way people paste it (upper case, colons,
     * spaces) and returns canonical lower-case hex, or null when it is not a
     * SHA-256 value.
     */
    fun normalize(text: String?): String? {
        if (text == null) return null
        val cleaned = text.trim().lowercase().replace(":", "").replace(" ", "").replace("-", "")
        if (cleaned.length != 64) return null
        if (cleaned.any { it !in HEX }) return null
        return cleaned
    }

    /** Constant-time comparison of two canonical fingerprints. */
    fun matches(expected: String, actual: String): Boolean =
        MessageDigest.isEqual(expected.toByteArray(Charsets.US_ASCII), actual.toByteArray(Charsets.US_ASCII))
}

object PairingLink {
    const val SUPPORTED_VERSION = 1

    class InvalidLink(message: String) : Exception(message)

    /** Parses `felix://pair?...`. Throws [InvalidLink] with a message fit to show the user. */
    fun parse(link: String): PairingInfo {
        val text = link.trim()
        val schemeEnd = text.indexOf("://")
        if (schemeEnd <= 0 || !text.substring(0, schemeEnd).equals("felix", ignoreCase = true)) {
            throw InvalidLink("This is not a Felix pairing link.")
        }
        val rest = text.substring(schemeEnd + 3)
        val queryStart = rest.indexOf('?')
        val target = (if (queryStart >= 0) rest.substring(0, queryStart) else rest).trim('/')
        if (!target.equals("pair", ignoreCase = true)) {
            throw InvalidLink("This Felix link is not a pairing link.")
        }
        val fragmentStart = rest.indexOf('#')
        val query = when {
            queryStart < 0 -> ""
            fragmentStart > queryStart -> rest.substring(queryStart + 1, fragmentStart)
            else -> rest.substring(queryStart + 1)
        }
        val params = parseQuery(query)
        val version = params["v"]?.toIntOrNull() ?: SUPPORTED_VERSION
        return build(
            version = version,
            scheme = params["s"] ?: "https",
            hosts = params["h"] ?: "",
            port = params["p"] ?: "",
            code = params["c"] ?: "",
            fingerprint = params["fp"],
            name = params["n"],
        )
    }

    /** Validates values typed into the manual pairing form (same rules as a link). */
    fun build(
        version: Int = SUPPORTED_VERSION,
        scheme: String,
        hosts: String,
        port: String,
        code: String,
        fingerprint: String?,
        name: String?,
    ): PairingInfo {
        if (version > SUPPORTED_VERSION) {
            throw InvalidLink("This pairing code needs a newer Felix Companion. Update the app and try again.")
        }
        if (version < 1) throw InvalidLink("Unknown pairing link version.")
        val cleanScheme = scheme.trim().lowercase()
        if (cleanScheme != "https" && cleanScheme != "http") {
            throw InvalidLink("Unsupported connection type \"$scheme\".")
        }
        val hostList = splitHosts(hosts)
        if (hostList.isEmpty()) throw InvalidLink("The laptop address is missing.")
        for (host in hostList) {
            if (!isPlausibleHost(host)) throw InvalidLink("\"$host\" is not a valid address.")
        }
        val portNumber = port.trim().toIntOrNull()
        if (portNumber == null || portNumber !in 1..65535) throw InvalidLink("The port must be a number between 1 and 65535.")
        val cleanCode = code.trim()
        if (cleanCode.length !in 4..64 || cleanCode.any { !(it.isLetterOrDigit() || it == '-' || it == '_') }) {
            throw InvalidLink("The pairing code is missing or malformed.")
        }
        var pin: String? = null
        if (cleanScheme == "https") {
            pin = Fingerprints.normalize(fingerprint)
                ?: throw InvalidLink("The security fingerprint is missing or malformed. Scan the QR code again.")
        }
        val display = name?.trim().orEmpty().ifEmpty { "Felix" }
        return PairingInfo(version, cleanScheme, hostList, portNumber, cleanCode, pin, display.take(80))
    }

    fun splitHosts(text: String): List<String> {
        val out = ArrayList<String>()
        for (part in text.split(',', ' ', '\n', ';')) {
            val host = part.trim()
            if (host.isNotEmpty() && host !in out) out.add(host)
        }
        return out
    }

    fun parseQuery(query: String): Map<String, String> {
        val out = LinkedHashMap<String, String>()
        for (pair in query.split('&')) {
            if (pair.isEmpty()) continue
            val eq = pair.indexOf('=')
            val rawKey = if (eq >= 0) pair.substring(0, eq) else pair
            val rawValue = if (eq >= 0) pair.substring(eq + 1) else ""
            val key = decode(rawKey)
            if (key.isNotEmpty() && key !in out) out[key] = decode(rawValue)
        }
        return out
    }

    private fun decode(text: String): String = try {
        URLDecoder.decode(text, "UTF-8")
    } catch (_: IllegalArgumentException) {
        text
    }

    private fun isPlausibleHost(host: String): Boolean {
        if (host.length > 253) return false
        val inner = if (host.startsWith("[") && host.endsWith("]")) host.substring(1, host.length - 1) else host
        if (inner.isEmpty()) return false
        return inner.all { it.isLetterOrDigit() || it == '.' || it == '-' || it == ':' || it == '_' || it == '%' }
    }

    /** `scheme://host:port` with IPv6 literals bracketed. */
    fun baseUrl(scheme: String, host: String, port: Int): String {
        val needsBrackets = host.contains(':') && !host.startsWith("[")
        val shown = if (needsBrackets) "[$host]" else host
        return "$scheme://$shown:$port"
    }
}
