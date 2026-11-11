package ai.felix.companion.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ProtocolTest {
    @Test
    fun eventsBodyMatchesTheDocument() {
        val event = Event("11111111-1111-1111-1111-111111111111", "message", 1794000000000L, mapOf("text" to "hi", "is_group" to false))
        val body = Json.parseObject(Wire.eventsBody(listOf(event)))
        val first = Json.asObject(body.list("events")[0])
        assertEquals("11111111-1111-1111-1111-111111111111", first["id"])
        assertEquals("message", first["type"])
        assertEquals(1794000000000L, first["ts"])
        assertEquals("hi", first.obj("data")["text"])
        assertEquals(event, Event.fromJson(first))
    }

    @Test
    fun parsesAcceptedIds() {
        assertEquals(setOf("a", "b"), Wire.parseAccepted("""{"accepted": ["a", "b"]}"""))
        assertTrue(Wire.parseAccepted("garbage").isEmpty())
    }

    @Test
    fun parsesPollResponse() {
        val commands = Wire.parsePoll(
            """{"commands": [{"id": 123, "type": "wa.reply", "args": {"chat_key": "com.whatsapp|x", "text": "On my way"}, "expires": 1794000060000},
                             {"id": "bad"}, {"type": "ping"}, {"id": 5, "type": "ping"}],
                "server_ts": 1794000000000}"""
        )
        assertEquals(2, commands.size)
        assertEquals(123L, commands[0].id)
        assertEquals("wa.reply", commands[0].type)
        assertEquals("On my way", commands[0].args["text"])
        assertFalse(commands[0].isExpired(1794000000000L))
        assertTrue(commands[0].isExpired(1794000060001L))
        assertEquals(0L, commands[1].expires)
        assertFalse(commands[1].isExpired(Long.MAX_VALUE))
        assertTrue(Wire.parsePoll("""{"commands": []}""").isEmpty())
        assertTrue(Wire.parsePoll("").isEmpty())
    }

    @Test
    fun resultsBodyHasOkAndErrorShapes() {
        val body = Wire.resultsBody(
            listOf(
                CommandResult.of(123, Outcome.ok("pong" to true)),
                CommandResult.of(124, Outcome.fail(Errors.NO_REPLY_ACTION, "No open notification for that chat")),
                CommandResult.of(125, Outcome.fail(Errors.UNSUPPORTED)),
            )
        )
        val results = Json.parseObject(body).list("results").map { Json.asObject(it) }
        assertEquals(mapOf("id" to 123L, "ok" to true, "data" to mapOf("pong" to true)), results[0])
        assertEquals(
            mapOf("id" to 124L, "ok" to false, "error" to "no_reply_action", "message" to "No open notification for that chat"),
            results[1]
        )
        assertEquals(mapOf("id" to 125L, "ok" to false, "error" to "unsupported"), results[2])
        val back = CommandResult.fromJson(results[1])!!
        assertEquals(Errors.NO_REPLY_ACTION, back.error)
        assertFalse(back.ok)
    }

    @Test
    fun pairRequestAndResponse() {
        val body = Json.parseObject(Wire.pairBody("482913", "dev-1", "Pixel 8", "Google Pixel 8", "Android 15", "0.1.0"))
        assertEquals(
            mapOf("code" to "482913", "device_id" to "dev-1", "name" to "Pixel 8", "model" to "Google Pixel 8", "os" to "Android 15", "app_version" to "0.1.0"),
            body
        )
        val response = Wire.parsePairResponse(
            """{"token": "tok", "device_id": "dev-1", "core": {"name": "VK's laptop", "version": "0.1.0"}, "poll_wait": 25}"""
        )!!
        assertEquals("tok", response.token)
        assertEquals("VK's laptop", response.coreName)
        assertEquals(25, response.pollWait)
        assertNull(Wire.parsePairResponse("""{"error": "invalid_code"}"""))
        assertEquals("invalid_code", Wire.errorCode("""{"error": "invalid_code"}"""))
        assertEquals(120, Wire.parsePairResponse("""{"token": "t", "poll_wait": 100000}""")!!.pollWait)
    }

    @Test
    fun chatAndApprovals() {
        assertEquals(mapOf("text" to "hello", "thread" to "phone"), Json.parseObject(Wire.chatBody("hello")))
        val reply = Wire.parseChatReply(
            Json.parseObject("""{"reply": "Done", "engine": "rules", "online": false, "approvals": [{"id": 7, "tool": "whatsapp_send", "preview": "To Ravi: hi", "risk": "outbound"}]}""")
        )
        assertEquals("Done", reply.reply)
        assertFalse(reply.online)
        assertEquals(Approval(7, "whatsapp_send", "To Ravi: hi", "outbound"), reply.approvals.single())
        assertEquals(1, Wire.parseApprovals("""{"approvals": [{"id": 9, "tool": "t", "preview": "p", "risk": "r", "created_at": 1}, {"tool": "no id"}]}""").size)
    }
}
