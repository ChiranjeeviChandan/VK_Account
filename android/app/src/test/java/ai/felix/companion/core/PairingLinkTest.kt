package ai.felix.companion.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class PairingLinkTest {
    private val fp = "ab".repeat(32)

    @Test
    fun parsesTheDocumentedExample() {
        val info = PairingLink.parse(
            "felix://pair?v=1&s=https&h=192.168.1.20,felix-laptop.local&p=8766&c=482913&fp=$fp&n=VK%27s%20laptop"
        )
        assertEquals(1, info.version)
        assertEquals("https", info.scheme)
        assertEquals(listOf("192.168.1.20", "felix-laptop.local"), info.hosts)
        assertEquals(8766, info.port)
        assertEquals("482913", info.code)
        assertEquals(fp, info.fingerprint)
        assertEquals("VK's laptop", info.name)
    }

    @Test
    fun defaultsAndPlusEncoding() {
        val info = PairingLink.parse("FELIX://pair/?h=10.0.0.5&p=8766&c=123456&fp=${fp.uppercase()}&n=My+Laptop")
        assertEquals("https", info.scheme)
        assertEquals(1, info.version)
        assertEquals(fp, info.fingerprint)
        assertEquals("My Laptop", info.name)
    }

    @Test
    fun httpNeedsNoFingerprint() {
        val info = PairingLink.parse("felix://pair?v=1&s=http&h=127.0.0.1&p=8766&c=111111")
        assertEquals("http", info.scheme)
        assertNull(info.fingerprint)
        assertEquals("Felix", info.name)
    }

    @Test
    fun rejectsBadLinks() {
        val bad = listOf(
            "https://example.com/pair?h=a&p=1&c=1234",
            "felix://settings?h=a&p=1&c=1234",
            "felix://pair?s=https&h=10.0.0.5&p=8766&c=123456",                    // https without a pin
            "felix://pair?s=https&h=10.0.0.5&p=8766&c=123456&fp=1234",            // short pin
            "felix://pair?s=ftp&h=10.0.0.5&p=8766&c=123456&fp=$fp",
            "felix://pair?h=&p=8766&c=123456&fp=$fp",
            "felix://pair?h=10.0.0.5&p=0&c=123456&fp=$fp",
            "felix://pair?h=10.0.0.5&p=99999&c=123456&fp=$fp",
            "felix://pair?h=10.0.0.5&p=8766&c=&fp=$fp",
            "felix://pair?h=bad host/..&p=8766&c=123456&fp=$fp",
            "felix://pair?v=2&h=10.0.0.5&p=8766&c=123456&fp=$fp",                 // newer protocol
            "not a link",
        )
        for (link in bad) {
            try {
                PairingLink.parse(link)
                fail("accepted: $link")
            } catch (_: PairingLink.InvalidLink) {
            }
        }
    }

    @Test
    fun manualFormUsesTheSameRules() {
        val info = PairingLink.build(
            scheme = "HTTPS", hosts = " 192.168.1.20 , felix.local\n192.168.1.20 ", port = " 8766 ",
            code = " 482913 ", fingerprint = "AB:".repeat(32).trimEnd(':'), name = null,
        )
        assertEquals(listOf("192.168.1.20", "felix.local"), info.hosts)
        assertEquals(fp, info.fingerprint)
        assertEquals("Felix", info.name)
    }

    @Test
    fun fingerprintHelpers() {
        // SHA-256("abc") from FIPS 180-2
        assertEquals(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            Fingerprints.sha256Hex("abc".toByteArray())
        )
        assertEquals(fp, Fingerprints.normalize(" " + fp.uppercase().chunked(2).joinToString(":") + " "))
        assertNull(Fingerprints.normalize("zz".repeat(32)))
        assertNull(Fingerprints.normalize(null))
        assertTrue(Fingerprints.matches(fp, fp))
        assertTrue(!Fingerprints.matches(fp, "cd".repeat(32)))
    }

    @Test
    fun baseUrlBracketsIpv6() {
        assertEquals("https://192.168.1.20:8766", PairingLink.baseUrl("https", "192.168.1.20", 8766))
        assertEquals("https://[fe80::1]:8766", PairingLink.baseUrl("https", "fe80::1", 8766))
        assertEquals("http://[::1]:80", PairingLink.baseUrl("http", "[::1]", 80))
    }
}
