package ai.felix.companion.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class OfflineCommandsTest {
    private fun parsed(text: String): ParsedCommand {
        val command = OfflineCommands.parse(text)
        assertNotNull("not understood: $text", command)
        return command!!
    }

    private fun check(text: String, type: String, args: Map<String, Any?>) {
        val command = parsed(text)
        assertEquals("type for: $text", type, command.type)
        assertEquals("args for: $text", args, command.args)
    }

    @Test
    fun flashlight() {
        check("turn on the flashlight", "torch", mapOf("on" to true))
        check("Flashlight off", "torch", mapOf("on" to false))
        check("hey Felix, switch the torch on please", "torch", mapOf("on" to true))
        check("torch", "torch", mapOf("on" to true))
        check("turn off torch.", "torch", mapOf("on" to false))
    }

    @Test
    fun calls() {
        check("call Ravi Kumar", "call.dial", mapOf("name" to "Ravi Kumar"))
        check("Call +91 98123 45678", "call.dial", mapOf("number" to "+919812345678"))
        check("dial 100", "call.dial", mapOf("number" to "100"))
        check("please call Mom now", "call.dial", mapOf("name" to "Mom"))
        check("ring Priya", "call.dial", mapOf("name" to "Priya"))
    }

    @Test
    fun ringMyPhoneIsNotACall() {
        check("ring my phone", "locate", mapOf("seconds" to 30))
        check("find my phone", "locate", mapOf("seconds" to 30))
        check("Where is my phone?", "locate", mapOf("seconds" to 30))
    }

    @Test
    fun textMessagesKeepTheBodyVerbatim() {
        check("text Ravi saying I'm running 10 minutes late", "sms.send", mapOf("name" to "Ravi", "text" to "I'm running 10 minutes late"))
        check("send a text to Ravi Kumar saying Are you coming?", "sms.send", mapOf("name" to "Ravi Kumar", "text" to "Are you coming?"))
        check("sms +91 98123 45678: Call me back", "sms.send", mapOf("to" to "+919812345678", "text" to "Call me back"))
        check("Send an SMS to Mom that says Landed safely.", "sms.send", mapOf("name" to "Mom", "text" to "Landed safely."))
        // Without a separator the split between name and message would be a guess, so it is refused.
        assertNull(OfflineCommands.parse("text Ravi Kumar I'm late"))
    }

    @Test
    fun openAppsAndLinks() {
        check("open WhatsApp", "app.open", mapOf("name" to "WhatsApp"))
        check("launch the camera app", "app.open", mapOf("name" to "camera"))
        check("open google maps", "app.open", mapOf("name" to "google maps"))
        check("open example.com", "url.open", mapOf("url" to "https://example.com"))
        check("open https://example.com/a?b=1", "url.open", mapOf("url" to "https://example.com/a?b=1"))
    }

    @Test
    fun alarms() {
        check("set an alarm for 6:30 am", "alarm.set", mapOf("hour" to 6, "minute" to 30))
        check("set alarm 18:45", "alarm.set", mapOf("hour" to 18, "minute" to 45))
        check("alarm at 6 am", "alarm.set", mapOf("hour" to 6, "minute" to 0))
        check("wake me up at 7 p.m.", "alarm.set", mapOf("hour" to 19, "minute" to 0))
        check("set an alarm for 12 am", "alarm.set", mapOf("hour" to 0, "minute" to 0))
        check("set an alarm for noon", "alarm.set", mapOf("hour" to 12, "minute" to 0))
        check("alarm 7 in the evening", "alarm.set", mapOf("hour" to 19, "minute" to 0))
        assertNull(OfflineCommands.parse("set an alarm for tomorrow sometime"))
        assertNull(OfflineCommands.parse("set an alarm for 25:00"))
        assertEquals("Alarm set for 06:30.", parsed("alarm 6:30").summary)
    }

    @Test
    fun clockParsing() {
        assertEquals(6 to 0, OfflineCommands.parseClock("6"))
        assertEquals(12 to 0, OfflineCommands.parseClock("12 pm"))
        assertEquals(0 to 5, OfflineCommands.parseClock("12:05am"))
        assertEquals(23 to 59, OfflineCommands.parseClock("23.59"))
        assertEquals(21 to 0, OfflineCommands.parseClock("9 at night"))
        assertEquals(6 to 0, OfflineCommands.parseClock("6 o'clock"))
        assertEquals(0 to 0, OfflineCommands.parseClock("midnight"))
        assertNull(OfflineCommands.parseClock("13 pm"))
        assertNull(OfflineCommands.parseClock("6:75"))
        assertNull(OfflineCommands.parseClock("soon"))
    }

    @Test
    fun timers() {
        check("set a timer for 10 minutes", "timer.set", mapOf("seconds" to 600))
        check("timer 90 seconds", "timer.set", mapOf("seconds" to 90))
        check("start a timer for 1 hour 30 minutes", "timer.set", mapOf("seconds" to 5400))
        check("5 minute timer", "timer.set", mapOf("seconds" to 300))
        check("set a timer for half an hour", "timer.set", mapOf("seconds" to 1800))
        check("timer 15", "timer.set", mapOf("seconds" to 900))
        check("set a timer for 1h 5m 10s", "timer.set", mapOf("seconds" to 3910))
        assertEquals("Timer set for 1 h 30 min.", parsed("timer 1 hour and 30 minutes").summary)
    }

    @Test
    fun durationParsing() {
        assertEquals(45, OfflineCommands.parseDuration("45s"))
        assertEquals(3600, OfflineCommands.parseDuration("an hour"))
        assertEquals(150, OfflineCommands.parseDuration("2.5 minutes"))
        assertNull(OfflineCommands.parseDuration("10 bananas"))
        assertNull(OfflineCommands.parseDuration("10 minutes after lunch"))
        assertNull(OfflineCommands.parseDuration("0 seconds"))
        assertNull(OfflineCommands.parseDuration("48 hours"))
    }

    @Test
    fun volumeAndRinger() {
        check("volume 40%", "volume", mapOf("stream" to "media", "level" to 40))
        check("set the ring volume to 80 percent", "volume", mapOf("stream" to "ring", "level" to 80))
        check("set alarm volume to 100", "volume", mapOf("stream" to "alarm", "level" to 100))
        check("volume 250", "volume", mapOf("stream" to "media", "level" to 100))
        check("mute", "volume", mapOf("stream" to "media", "level" to 0))
        check("max volume", "volume", mapOf("stream" to "media", "level" to 100))
        check("silent", "ringer", mapOf("mode" to "silent"))
        check("put my phone on silent", "ringer", mapOf("mode" to "silent"))
        check("vibrate mode", "ringer", mapOf("mode" to "vibrate"))
        check("ringer on", "ringer", mapOf("mode" to "normal"))
        check("turn do not disturb on", "dnd", mapOf("on" to true))
        check("dnd off", "dnd", mapOf("on" to false))
        check("brightness 60%", "brightness", mapOf("level" to 60))
    }

    @Test
    fun statusAndNavigation() {
        check("battery", "status", emptyMap())
        check("phone status", "status", emptyMap())
        check("how much battery is left?", "status", emptyMap())
        check("go home", "ui.global", mapOf("action" to "home"))
        check("lock the screen", "ui.global", mapOf("action" to "lock"))
        check("take a screenshot", "ui.global", mapOf("action" to "screenshot"))
        check("next song", "media", mapOf("action" to "next"))
        check("pause", "media", mapOf("action" to "play_pause"))
    }

    @Test
    fun arbitraryInputNeverThrows() {
        val odd = listOf(
            "\u0000", "call", "call ", "text :", "open", "open  ", "timer", "alarm", "volume", "volume -5",
            "set an alarm for 99999999999999999999", "timer 99999999999999999999 minutes", "call +", "sms : :",
            "\uD83D\uDE00 call \uD83D\uDE00", "CALL \u0130stanbul office", "a".repeat(5000), "open " + "x".repeat(3000),
            "text " + "y".repeat(2000) + " saying hi", "felix, ", "hey felix", "please", "volume 50 % %", "(((", "\\d+",
        )
        for (text in odd) OfflineCommands.parse(text)
        val random = java.util.Random(42)
        val alphabet = "abcdefghij 0123456789:.,%+-?!'\"()\n\t\u00e9\u0130"
        repeat(3000) {
            val builder = StringBuilder()
            repeat(random.nextInt(40)) { builder.append(alphabet[random.nextInt(alphabet.length)]) }
            val prefix = listOf("", "call ", "text ", "open ", "set alarm ", "timer ", "volume ")[random.nextInt(7)]
            OfflineCommands.parse(prefix + builder)
        }
    }

    @Test
    fun questionsForCoreAreNotPhoneCommands() {
        for (text in listOf(
            "what's on my calendar today",
            "summarise my unread emails",
            "reply to Ravi on WhatsApp saying yes",
            "",
            "   ",
            "what is the battery capacity of a Tesla",
        )) {
            assertNull("should not be handled offline: $text", OfflineCommands.parse(text))
        }
    }
}
