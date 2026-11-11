package ai.felix.companion.net

import ai.felix.companion.core.Fingerprints
import ai.felix.companion.core.PairingLink
import java.io.IOException
import java.net.HttpURLConnection
import java.net.Proxy
import java.net.URL
import java.nio.charset.StandardCharsets
import java.security.SecureRandom
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.X509TrustManager

class HttpResult(val code: Int, val body: String) {
    val ok: Boolean get() = code in 200..299
}

/** None of the known addresses of Felix Core answered. */
class CoreUnreachable(message: String, cause: Throwable? = null) : IOException(message, cause)

/** The server presented a certificate other than the one pinned at pairing. */
class PinMismatch(message: String) : CertificateException(message)

/** The connection could not be established, so nothing was sent and another host may be tried. */
private class ConnectFailed(val host: String, cause: IOException) : IOException(cause.message, cause)

/**
 * Trusts exactly one certificate: the one whose SHA-256 fingerprint was shown
 * in the pairing QR code. Certificate authorities play no part.
 */
private class PinningTrustManager(private val pin: String) : X509TrustManager {
    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        throw CertificateException("Client certificates are not accepted")
    }

    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        val leaf = chain?.firstOrNull() ?: throw CertificateException("The server sent no certificate")
        val actual = Fingerprints.sha256Hex(leaf.encoded)
        if (!Fingerprints.matches(pin, actual)) {
            throw PinMismatch("This is not the Felix Core this phone was paired with (certificate fingerprint differs).")
        }
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
}

private object Tls {
    private val factories = HashMap<String, SSLSocketFactory>()

    @Synchronized
    fun socketFactory(pin: String): SSLSocketFactory = factories.getOrPut(pin) {
        val context = SSLContext.getInstance("TLS")
        context.init(null, arrayOf(PinningTrustManager(pin)), SecureRandom())
        context.socketFactory
    }

    /**
     * Host names are not checked on pinned connections: the pin already
     * identifies Core, and its address changes with the network. This verifier
     * is only ever attached to a connection that uses [PinningTrustManager].
     */
    val pinnedHost = HostnameVerifier { _, _ -> true }
}

/** The stored pairing, as the network layer needs it. Implemented by the app's preferences. */
interface LinkConfig {
    val scheme: String
    val hosts: List<String>
    val port: Int
    val fingerprint: String?
    val token: String?

    /** The address that answered last; tried first on the next request. */
    var activeHost: String?
}

/** Where and how to reach one Felix Core. */
data class Endpoint(
    val scheme: String,
    val hosts: List<String>,
    val port: Int,
    val fingerprint: String?,
    val token: String?,
)

class CoreClient(private val config: LinkConfig) {

    private fun endpoint(): Endpoint =
        Endpoint(config.scheme, orderedHosts(config.hosts, config.activeHost), config.port, config.fingerprint, config.token)

    /**
     * Sends one request to Core, trying the last working address first and the
     * other known addresses if it cannot connect. Throws [CoreUnreachable]
     * when no address accepts a connection.
     */
    @Throws(IOException::class)
    fun request(method: String, path: String, body: String? = null, readTimeoutMs: Int = 15_000): HttpResult {
        val target = endpoint()
        if (target.hosts.isEmpty()) throw CoreUnreachable("This phone is not paired with Felix Core.")
        var lastError: IOException? = null
        for ((index, host) in target.hosts.withIndex()) {
            try {
                val result = exchange(target, host, method, path, body, connectTimeout(index), readTimeoutMs)
                config.activeHost = host
                return result
            } catch (e: ConnectFailed) {
                lastError = preferPinMismatch(lastError, e)
            }
        }
        throw unreachable(lastError)
    }

    /**
     * POSTs [body] and hands every line of a streamed response to [onLine].
     * Returns the HTTP status; lines are delivered only for a 200.
     */
    @Throws(IOException::class)
    fun stream(path: String, body: String, readTimeoutMs: Int = 120_000, onLine: (String) -> Unit): HttpResult {
        val target = endpoint()
        if (target.hosts.isEmpty()) throw CoreUnreachable("This phone is not paired with Felix Core.")
        var lastError: IOException? = null
        for ((index, host) in target.hosts.withIndex()) {
            val connection = try {
                open(target, host, path)
            } catch (e: IOException) {
                lastError = e
                continue
            }
            try {
                val bytes = body.toByteArray(StandardCharsets.UTF_8)
                prepare(connection, target.token, "POST", connectTimeout(index), readTimeoutMs)
                connection.setRequestProperty("Accept", "text/event-stream")
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                connection.doOutput = true
                connection.setFixedLengthStreamingMode(bytes.size)
                try {
                    connection.connect()
                } catch (e: IOException) {
                    lastError = preferPinMismatch(lastError, ConnectFailed(host, e))
                    continue
                }
                config.activeHost = host
                connection.outputStream.use { it.write(bytes) }
                val code = connection.responseCode
                if (code != 200) return HttpResult(code, readBody(connection, code))
                connection.inputStream.bufferedReader(StandardCharsets.UTF_8).use { reader ->
                    while (true) {
                        val line = reader.readLine() ?: break
                        onLine(line)
                    }
                }
                return HttpResult(200, "")
            } finally {
                connection.disconnect()
            }
        }
        throw unreachable(lastError)
    }

    companion object {
        /** The last working host first, then the others in pairing order. */
        fun orderedHosts(hosts: List<String>, active: String?): List<String> {
            if (active.isNullOrEmpty() || active !in hosts) return hosts
            return listOf(active) + hosts.filter { it != active }
        }

        private fun connectTimeout(index: Int): Int = if (index == 0) 6_000 else 4_000

        /**
         * One request to one address. Used directly for pairing, where the
         * caller walks the candidate addresses itself. Throws [CoreUnreachable]
         * if the connection cannot be established.
         */
        @Throws(IOException::class)
        fun requestOnce(
            target: Endpoint,
            host: String,
            method: String,
            path: String,
            body: String?,
            connectTimeoutMs: Int = 6_000,
            readTimeoutMs: Int = 15_000,
        ): HttpResult = try {
            exchange(target, host, method, path, body, connectTimeoutMs, readTimeoutMs)
        } catch (e: ConnectFailed) {
            throw unreachable(e)
        }

        private fun unreachable(cause: IOException?): CoreUnreachable {
            val root = if (cause is ConnectFailed) cause.cause else cause
            val reason = when {
                isPinMismatch(root) -> "Security check failed: the laptop's certificate is not the one this phone was paired with."
                root?.message.isNullOrBlank() -> "Felix Core is unreachable."
                else -> "Felix Core is unreachable (${root.message})."
            }
            return CoreUnreachable(reason, root)
        }

        private fun isPinMismatch(error: Throwable?): Boolean {
            var current = error
            var depth = 0
            while (current != null && depth < 8) {
                if (current is PinMismatch) return true
                current = current.cause
                depth++
            }
            return false
        }

        /** A certificate mismatch is the error worth reporting, even if a later host merely timed out. */
        private fun preferPinMismatch(previous: IOException?, latest: IOException): IOException =
            if (previous != null && isPinMismatch(previous) && !isPinMismatch(latest)) previous else latest

        @Throws(IOException::class)
        private fun open(target: Endpoint, host: String, path: String): HttpURLConnection {
            if (target.scheme != "https" && target.scheme != "http") {
                throw IOException("Unsupported scheme ${target.scheme}")
            }
            @Suppress("DEPRECATION") // URL(String) is the form available on every supported Android version
            val url = URL(PairingLink.baseUrl(target.scheme, host, target.port) + path)
            // Always direct: Core is a machine on the owner's own network, never behind a web proxy.
            val connection = url.openConnection(Proxy.NO_PROXY) as HttpURLConnection
            if (connection is HttpsURLConnection) {
                val pin = target.fingerprint
                    ?: throw IOException("No certificate fingerprint is stored for this Felix Core. Pair again.")
                connection.sslSocketFactory = Tls.socketFactory(pin)
                connection.hostnameVerifier = Tls.pinnedHost
            }
            connection.instanceFollowRedirects = false
            connection.useCaches = false
            return connection
        }

        private fun prepare(connection: HttpURLConnection, token: String?, method: String, connectTimeoutMs: Int, readTimeoutMs: Int) {
            connection.requestMethod = method
            connection.connectTimeout = connectTimeoutMs
            connection.readTimeout = readTimeoutMs
            connection.setRequestProperty("User-Agent", "FelixCompanion")
            if (!token.isNullOrEmpty()) connection.setRequestProperty("Authorization", "Bearer $token")
        }

        private fun readBody(connection: HttpURLConnection, code: Int): String {
            val stream = (if (code >= 400) connection.errorStream else connection.inputStream) ?: return ""
            return stream.use { String(it.readBytes(), StandardCharsets.UTF_8) }
        }

        @Throws(IOException::class)
        private fun exchange(
            target: Endpoint,
            host: String,
            method: String,
            path: String,
            body: String?,
            connectTimeoutMs: Int,
            readTimeoutMs: Int,
        ): HttpResult {
            val connection = try {
                open(target, host, path)
            } catch (e: IOException) {
                throw ConnectFailed(host, e)
            }
            try {
                prepare(connection, target.token, method, connectTimeoutMs, readTimeoutMs)
                connection.setRequestProperty("Accept", "application/json")
                val bytes = body?.toByteArray(StandardCharsets.UTF_8)
                if (bytes != null) {
                    connection.doOutput = true
                    connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
                    connection.setFixedLengthStreamingMode(bytes.size)
                }
                try {
                    connection.connect()
                } catch (e: IOException) {
                    throw ConnectFailed(host, e)
                }
                if (bytes != null) connection.outputStream.use { it.write(bytes) }
                val code = connection.responseCode
                return HttpResult(code, readBody(connection, code))
            } finally {
                connection.disconnect()
            }
        }
    }
}
