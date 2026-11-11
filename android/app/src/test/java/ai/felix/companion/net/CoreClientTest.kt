package ai.felix.companion.net

import ai.felix.companion.core.Fingerprints
import ai.felix.companion.core.Json
import ai.felix.companion.core.SseParser
import ai.felix.companion.core.Wire
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.charset.StandardCharsets
import java.security.KeyFactory
import java.security.KeyStore
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import java.security.spec.PKCS8EncodedKeySpec
import java.util.Base64
import java.util.concurrent.CopyOnWriteArrayList
import javax.net.ssl.KeyManagerFactory
import javax.net.ssl.SSLContext
import kotlin.concurrent.thread

/**
 * Drives the real [CoreClient] against a small HTTPS server on the loopback
 * interface: certificate pinning, bearer authentication, address fail-over and
 * streamed replies.
 */
class CoreClientTest {
    private class Request(val method: String, val path: String, val headers: Map<String, String>, val body: String)

    private class Response(val status: Int, val body: String = "", val lines: List<String>? = null)

    private class FakeConfig(
        override val scheme: String,
        override val hosts: List<String>,
        override val port: Int,
        override val fingerprint: String?,
        override val token: String?,
    ) : LinkConfig {
        override var activeHost: String? = null
    }

    /** A stand-in for Felix Core that speaks just enough HTTP for these tests. */
    private class TestCore(secure: Boolean, private val handler: (Request) -> Response) : AutoCloseable {
        private val listener: ServerSocket
        val requests = CopyOnWriteArrayList<Request>()

        @Volatile
        private var open = true

        val port: Int get() = listener.localPort

        init {
            val loopback = InetAddress.getByName("127.0.0.1")
            listener = if (secure) {
                serverContext().serverSocketFactory.createServerSocket(0, 16, loopback)
            } else {
                ServerSocket(0, 16, loopback)
            }
            thread(isDaemon = true, name = "test-core") {
                while (open) {
                    val client = try {
                        listener.accept()
                    } catch (_: IOException) {
                        break
                    }
                    thread(isDaemon = true) { serve(client) }
                }
            }
        }

        private fun serve(client: Socket) {
            try {
                client.use { socket ->
                    socket.soTimeout = 10_000
                    val input = BufferedInputStream(socket.getInputStream())
                    val head = readHead(input) ?: return
                    val headLines = head.split("\r\n")
                    val start = headLines[0].split(" ")
                    val headers = HashMap<String, String>()
                    for (line in headLines.drop(1)) {
                        val colon = line.indexOf(':')
                        if (colon > 0) headers[line.substring(0, colon).trim().lowercase()] = line.substring(colon + 1).trim()
                    }
                    val length = headers["content-length"]?.toIntOrNull() ?: 0
                    val body = ByteArray(length)
                    var read = 0
                    while (read < length) {
                        val n = input.read(body, read, length - read)
                        if (n < 0) break
                        read += n
                    }
                    val request = Request(start[0], start.getOrElse(1) { "" }, headers, String(body, 0, read, StandardCharsets.UTF_8))
                    requests.add(request)
                    val response = handler(request)
                    val out = socket.getOutputStream()
                    if (response.lines != null) {
                        out.write("HTTP/1.1 ${response.status} OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n".toByteArray())
                        for (line in response.lines) {
                            out.write((line + "\n").toByteArray(StandardCharsets.UTF_8))
                            out.flush()
                        }
                    } else {
                        val bytes = response.body.toByteArray(StandardCharsets.UTF_8)
                        out.write(
                            ("HTTP/1.1 ${response.status} Status\r\nContent-Type: application/json\r\n" +
                                "Content-Length: ${bytes.size}\r\nConnection: close\r\n\r\n").toByteArray()
                        )
                        out.write(bytes)
                    }
                    out.flush()
                }
            } catch (_: IOException) {
                // Includes TLS handshakes the client aborted (the wrong-pin test).
            }
        }

        private fun readHead(input: InputStream): String? {
            val buffer = ByteArrayOutputStream()
            var matched = 0
            val end = byteArrayOf(13, 10, 13, 10)
            while (matched < 4) {
                val b = input.read()
                if (b < 0) return null
                buffer.write(b)
                matched = if (b.toByte() == end[matched]) matched + 1 else if (b == 13) 1 else 0
                if (buffer.size() > 64 * 1024) return null
            }
            return String(buffer.toByteArray(), StandardCharsets.ISO_8859_1).trimEnd()
        }

        override fun close() {
            open = false
            listener.close()
        }
    }

    private val servers = ArrayList<TestCore>()

    private fun core(secure: Boolean = true, handler: (Request) -> Response): TestCore =
        TestCore(secure, handler).also { servers.add(it) }

    @After
    fun stopServers() {
        servers.forEach { it.close() }
        servers.clear()
    }

    private fun config(server: TestCore, hosts: List<String> = listOf("127.0.0.1"), pin: String? = PIN, token: String? = "tok-123", scheme: String = "https") =
        FakeConfig(scheme, hosts, server.port, pin, token)

    @Test
    fun theEmbeddedCertificateHasTheExpectedFingerprint() {
        assertEquals(PIN, Fingerprints.sha256Hex(certificate().encoded))
    }

    @Test
    fun pinnedRequestSucceedsAndCarriesTheBearerToken() {
        val server = core { Response(200, """{"core": {"name": "Test Core", "version": "0.1.0"}, "online": true, "mode": "auto"}""") }
        val settings = config(server)
        val result = CoreClient(settings).request("GET", "/api/phone/hello")
        assertTrue(result.ok)
        assertEquals("Test Core", Json.asObject(Json.parseObject(result.body)["core"])["name"])
        val seen = server.requests.single()
        assertEquals("GET", seen.method)
        assertEquals("/api/phone/hello", seen.path)
        assertEquals("Bearer tok-123", seen.headers["authorization"])
        assertEquals("127.0.0.1", settings.activeHost)
    }

    @Test
    fun aDifferentCertificateIsRefusedBeforeAnythingIsSent() {
        val server = core { Response(200, "{}") }
        val client = CoreClient(config(server, pin = "00".repeat(32)))
        try {
            client.request("POST", "/api/phone/events", Wire.eventsBody(emptyList()))
            fail("a server with the wrong certificate was trusted")
        } catch (e: CoreUnreachable) {
            assertTrue(e.message.orEmpty(), e.message.orEmpty().contains("certificate"))
        }
        assertTrue("the request must not reach a server that failed the pin check", server.requests.isEmpty())
    }

    @Test
    fun httpsWithoutAStoredFingerprintIsNeverAttempted() {
        val server = core { Response(200, "{}") }
        try {
            CoreClient(config(server, pin = null)).request("GET", "/api/phone/hello")
            fail("connected over HTTPS without a pin")
        } catch (_: CoreUnreachable) {
        }
        assertTrue(server.requests.isEmpty())
    }

    @Test
    fun postDeliversTheJsonBody() {
        val server = core { Response(200, """{"accepted": ["a"]}""") }
        val body = """{"events":[{"id":"a","type":"status","ts":1,"data":{"text":"héllo"}}]}"""
        val result = CoreClient(config(server)).request("POST", "/api/phone/events", body)
        assertEquals(setOf("a"), Wire.parseAccepted(result.body))
        val seen = server.requests.single()
        assertEquals("POST", seen.method)
        assertEquals(body, seen.body)
        assertTrue(seen.headers["content-type"].orEmpty().startsWith("application/json"))
    }

    @Test
    fun errorStatusesAreReturnedNotThrown() {
        val server = core { Response(401, """{"error": "unauthorized"}""") }
        val result = CoreClient(config(server)).request("GET", "/api/phone/poll?wait=1")
        assertFalse(result.ok)
        assertEquals(401, result.code)
        assertEquals("unauthorized", Wire.errorCode(result.body))
    }

    @Test
    fun failsOverToTheNextAddressAndRemembersIt() {
        val server = core { Response(200, "{}") }
        // 127.0.0.2 has nothing listening on this port: the server is bound to 127.0.0.1 only.
        val settings = config(server, hosts = listOf("127.0.0.2", "127.0.0.1"))
        val client = CoreClient(settings)
        assertTrue(client.request("GET", "/api/phone/hello").ok)
        assertEquals("127.0.0.1", settings.activeHost)
        assertTrue(client.request("GET", "/api/phone/hello").ok)
        assertEquals(2, server.requests.size)
        assertEquals(listOf("127.0.0.1", "127.0.0.2"), CoreClient.orderedHosts(settings.hosts, settings.activeHost))
        assertEquals(listOf("a", "b"), CoreClient.orderedHosts(listOf("a", "b"), "gone"))
        assertEquals(listOf("a", "b"), CoreClient.orderedHosts(listOf("a", "b"), null))
    }

    @Test
    fun reportsUnreachableWhenNoAddressAnswers() {
        val server = core { Response(200, "{}") }
        val port = server.port
        server.close()
        val settings = FakeConfig("https", listOf("127.0.0.1"), port, PIN, "tok")
        try {
            CoreClient(settings).request("GET", "/api/phone/hello")
            fail("a closed port answered")
        } catch (_: CoreUnreachable) {
        }
        assertNull(settings.activeHost)
        try {
            CoreClient(FakeConfig("https", emptyList(), port, PIN, "tok")).request("GET", "/api/phone/hello")
            fail("an unpaired client sent a request")
        } catch (_: CoreUnreachable) {
        }
    }

    @Test
    fun streamedRepliesArriveLineByLine() {
        val server = core {
            Response(
                200,
                lines = listOf(
                    "event: delta", "data: {\"text\": \"Hel\"}", "",
                    "event: delta", "data: {\"text\": \"lo\"}", "",
                    "event: done", "data: {\"reply\": \"Hello\", \"engine\": \"rules\", \"online\": false, \"approvals\": []}", "",
                )
            )
        }
        val events = ArrayList<Pair<String, String>>()
        val parser = SseParser { name, data -> events.add(name to data) }
        val result = CoreClient(config(server)).stream("/api/chat/stream", Wire.chatBody("hi")) { parser.feedLine(it) }
        parser.finish()
        assertEquals(200, result.code)
        assertEquals(listOf("delta", "delta", "done"), events.map { it.first })
        assertEquals("Hello", Wire.parseChatReply(Json.parseObject(events.last().second)).reply)
        val seen = server.requests.single()
        assertEquals("POST", seen.method)
        assertEquals(mapOf("text" to "hi", "thread" to "phone"), Json.parseObject(seen.body))
        assertEquals("text/event-stream", seen.headers["accept"])
    }

    @Test
    fun aStreamThatIsNotAvailableReturnsItsStatus() {
        val server = core { Response(404, """{"error": "not_found"}""") }
        var lines = 0
        val result = CoreClient(config(server)).stream("/api/chat/stream", "{}") { lines++ }
        assertEquals(404, result.code)
        assertEquals(0, lines)
        assertEquals("not_found", Wire.errorCode(result.body))
    }

    @Test
    fun plainHttpWorksWhenThePairingAsksForIt() {
        val server = core(secure = false) { Response(200, """{"ok": true}""") }
        val result = CoreClient(config(server, pin = null, scheme = "http")).request("GET", "/api/phone/hello")
        assertTrue(result.ok)
        assertEquals("Bearer tok-123", server.requests.single().headers["authorization"])
    }

    @Test
    fun pairingTalksToOneAddressWithoutAToken() {
        val server = core { request ->
            val code = Json.parseObject(request.body)["code"]
            if (code == "482913") {
                Response(200, """{"token": "new-token", "device_id": "dev-1", "core": {"name": "Test Core", "version": "0.1.0"}, "poll_wait": 25}""")
            } else {
                Response(403, """{"error": "invalid_code"}""")
            }
        }
        val target = Endpoint("https", listOf("127.0.0.1"), server.port, PIN, null)
        val good = CoreClient.requestOnce(target, "127.0.0.1", "POST", "/api/phone/pair", Wire.pairBody("482913", "dev-1", "Pixel", "Google Pixel", "Android 15", "0.1.0"))
        assertEquals("new-token", Wire.parsePairResponse(good.body)!!.token)
        assertNull(server.requests.last().headers["authorization"])
        val bad = CoreClient.requestOnce(target, "127.0.0.1", "POST", "/api/phone/pair", Wire.pairBody("000000", "dev-1", "Pixel", "Google Pixel", "Android 15", "0.1.0"))
        assertEquals(403, bad.code)
        try {
            CoreClient.requestOnce(target, "127.0.0.2", "POST", "/api/phone/pair", "{}")
            fail("an address with no server answered")
        } catch (_: CoreUnreachable) {
        }
    }

    companion object {
        // A throwaway self-signed certificate and its key, generated for this test only.
        // They protect nothing: the test server and client both live inside this test.
        private const val TEST_CERTIFICATE =
            "MIIByjCCAW+gAwIBAgIUQm9PNOahkM7y5Npm8IAAa7DUej4wCgYIKoZIzj0EAwIwKzEpMCcGA1UE" +
                "AwwgRmVsaXggQ29yZSB0ZXN0IChub3QgYSByZWFsIGtleSkwIBcNMjYxMTExMjEzNTI2WhgPMjEy" +
                "NjEwMTgyMTM1MjZaMCsxKTAnBgNVBAMMIEZlbGl4IENvcmUgdGVzdCAobm90IGEgcmVhbCBrZXkp" +
                "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE0SlxSI1t8L0YWt3GlHO1avVqM23uPFW8puLA4vVy" +
                "66znNqbuyw7p1m+Ka77bQW+PcW2BAIv0zwvoZlT1Kcj26qNvMG0wHQYDVR0OBBYEFK8PVhuy7Z/y" +
                "KG2x7PFWX/xDtOrTMB8GA1UdIwQYMBaAFK8PVhuy7Z/yKG2x7PFWX/xDtOrTMA8GA1UdEwEB/wQF" +
                "MAMBAf8wGgYDVR0RBBMwEYcEfwAAAYIJbG9jYWxob3N0MAoGCCqGSM49BAMCA0kAMEYCIQD0IQTL" +
                "dYfOsFeFryc2o5Teu1e+Wxp5qyfQl6LlGjRI5gIhAMyMFtPWeKKbymcdiPu6FZ13Xs8QVLhQ72Hc" +
                "AFZa/FWF"
        private const val TEST_KEY =
            "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgzr1ZoSQvxU1bUCq6HmnqXh6FxHhu" +
                "WjSiR2LhUwMLF4KhRANCAATRKXFIjW3wvRha3caUc7Vq9Wozbe48Vbym4sDi9XLrrOc2pu7LDunW" +
                "b4prvttBb49xbYEAi/TPC+hmVPUpyPbq"
        private const val PIN = "5357e1c73cdaab245b1167785eb105043a1d73c5a04b734cf7becdc1955e9358"

        private fun certificate(): X509Certificate {
            val bytes = Base64.getDecoder().decode(TEST_CERTIFICATE)
            return CertificateFactory.getInstance("X.509").generateCertificate(bytes.inputStream()) as X509Certificate
        }

        private fun serverContext(): SSLContext {
            val key = KeyFactory.getInstance("EC").generatePrivate(PKCS8EncodedKeySpec(Base64.getDecoder().decode(TEST_KEY)))
            val password = "test".toCharArray()
            val store = KeyStore.getInstance(KeyStore.getDefaultType())
            store.load(null, null)
            store.setKeyEntry("felix", key, password, arrayOf(certificate()))
            val keys = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm())
            keys.init(store, password)
            val context = SSLContext.getInstance("TLS")
            context.init(keys.keyManagers, null, null)
            return context
        }
    }
}
