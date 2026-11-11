package ai.felix.companion.core

/** A phone action understood without Felix Core. [summary] is what the overlay says while doing it. */
data class ParsedCommand(val type: String, val args: Map<String, Any?>, val summary: String)

/**
 * The phone's own command grammar. It only covers actions the phone can carry
 * out by itself, so the overlay stays useful when the laptop is unreachable.
 * Commands that name a person carry `name` instead of a number; the caller
 * resolves it against the address book.
 */
object OfflineCommands {
    const val HELP =
        "Offline I can still: turn the flashlight on or off, call or text someone, open an app, " +
            "set an alarm or timer, change the volume, switch to silent or vibrate, and ring your phone."

    private val WAKE = Regex("""^(?:(?:hey|ok|okay|hi)\s+)?felix[\s,:]+""", RegexOption.IGNORE_CASE)
    private val POLITE_START = Regex("""^(?:please|kindly|can you|could you|would you)\s+""", RegexOption.IGNORE_CASE)
    private val POLITE_END = Regex("""\s*(?:,?\s*please)?[\s.!?]*$""", RegexOption.IGNORE_CASE)

    private val LOCATE = Regex("""^(?:find|locate|ring|where(?:'s| is))\s+(?:my |the )?phone$""")
    private val STATUS = Regex(
        """^(?:(?:phone |device )?status|(?:phone )?battery(?: level| status| percentage)?|how much battery(?: do i have| is left)?)$"""
    )
    private val TORCH_A = Regex("""^(?:turn |switch |put )?(?:the |my )?(?:flashlight|flash light|torch|flash)\s+(on|off)$""")
    private val TORCH_B = Regex("""^(?:turn|switch|put)\s+(on|off)\s+(?:the |my )?(?:flashlight|flash light|torch|flash)$""")
    private val TORCH_C = Regex("""^(?:flashlight|flash light|torch)$""")
    private val PHONE_MODE = """(?:put|set|switch|turn)\s+(?:my |the )?phone\s+(?:on|to|in|into)\s+"""
    private val SILENT = Regex("""^(?:silent(?: mode)?|silence(?: (?:my |the )?phone)?|mute (?:my |the )?phone|${PHONE_MODE}silent(?: mode)?)$""")
    private val VIBRATE = Regex("""^(?:vibrate(?: mode)?|vibration(?: mode)?|${PHONE_MODE}vibrate(?: mode)?)$""")
    private val NORMAL = Regex(
        """^(?:ring(?:er)? (?:on|mode)|normal mode|sound on|unmute(?: (?:my |the )?phone)?|unsilence(?: (?:my |the )?phone)?|${PHONE_MODE}(?:ring|normal|sound|loud)(?: mode)?)$"""
    )
    private val DND_A = Regex("""^(?:turn |switch )?(?:do not disturb|don't disturb|dnd)\s+(on|off)$""")
    private val DND_B = Regex("""^(?:turn|switch)\s+(on|off)\s+(?:do not disturb|don't disturb|dnd)$""")
    private val VOLUME = Regex(
        """^(?:set |turn |change |put )?(?:the )?(?:(media|music|ring|ringer|ringtone|alarm|notification) )?volume\s+(?:to |at )?(\d{1,3})\s?(?:%|percent)?$"""
    )
    private val VOLUME_MAX = Regex("""^(?:(?:max|full|maximum) volume|volume (?:max|full|maximum))$""")
    private val MUTE = Regex("""^mute(?: (?:the )?(?:volume|sound|music|media))?$""")
    private val BRIGHTNESS = Regex("""^(?:set |turn |change )?(?:the )?(?:screen )?brightness\s+(?:to |at )?(\d{1,3})\s?(?:%|percent)?$""")
    private val ALARM = Regex("""^(?:set |create |add |make )?(?:an? |the |my )?alarm\s+(?:for |at )?(.+)$""")
    private val WAKE_ME = Regex("""^wake me(?: up)?\s+(?:at |by )?(.+)$""")
    private val TIMER_A = Regex("""^(?:set |start |create )?(?:an? |the )?(?:timer|countdown)\s+(?:for |of )?(.+)$""")
    private val TIMER_B = Regex("""^(?:set |start |create )?(?:an? )?(.+?)\s+(?:timer|countdown)$""")
    private val CALL = Regex("""^(?:call|dial|phone|ring)\s+(.+)$""", RegexOption.IGNORE_CASE)
    private val SMS = Regex(
        """^(?:send\s+)?(?:an?\s+)?(?:sms|text message|text|message)\s+(?:to\s+)?(.+?)\s*(?:\s+saying|\s+that says|\s+to say|\s+and say|:)\s+(.+)$""",
        setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
    )
    private val OPEN = Regex("""^(?:open|launch|start|go to)\s+(?:the\s+)?(.+?)(?:\s+app)?$""", RegexOption.IGNORE_CASE)
    private val CALL_TAIL = Regex("""\s+(?:now|on speaker)$""", RegexOption.IGNORE_CASE)
    private val URL = Regex("""^(?:https?://\S+|[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?:/\S*)?)$""")
    private val GLOBALS: List<Pair<Regex, Pair<String, String>>> = listOf(
        Regex("""^(?:go )?home$|^go to (?:the )?home ?screen$""") to ("home" to "Going home."),
        Regex("""^(?:go )?back$""") to ("back" to "Going back."),
        Regex("""^(?:show |open )?(?:recent apps|recents)$""") to ("recents" to "Showing recent apps."),
        Regex("""^lock(?: (?:the |my )?(?:phone|screen))?$""") to ("lock" to "Locking the screen."),
        Regex("""^(?:take (?:a )?)?screenshot$""") to ("screenshot" to "Taking a screenshot."),
        Regex("""^(?:show |open )?notifications$""") to ("notifications" to "Opening notifications."),
    )
    private val MEDIA: List<Pair<Regex, Pair<String, String>>> = listOf(
        Regex("""^(?:play|resume|pause)(?: (?:the )?music)?$|^play/pause$""") to ("play_pause" to "Play / pause."),
        Regex("""^(?:next|skip)(?: (?:song|track))?$""") to ("next" to "Next track."),
        Regex("""^(?:previous|prev|last)(?: (?:song|track))?$""") to ("previous" to "Previous track."),
        Regex("""^stop(?: (?:the )?music)?$""") to ("stop" to "Stopping playback."),
    )

    fun parse(input: String): ParsedCommand? {
        // `spoken` keeps the user's exact words (needed for message bodies);
        // `plain` additionally drops a trailing "please" and end punctuation.
        var spoken = input.trim()
        spoken = WAKE.replace(spoken, "")
        spoken = POLITE_START.replace(spoken, "").trim()
        val plain = POLITE_END.replace(spoken, "").trim()
        if (plain.isEmpty()) return null
        val lower = plain.lowercase()

        if (LOCATE.matches(lower)) return ParsedCommand("locate", mapOf("seconds" to 30), "Ringing your phone.")
        if (STATUS.matches(lower)) return ParsedCommand("status", emptyMap(), "Checking the phone.")

        (TORCH_A.matchEntire(lower) ?: TORCH_B.matchEntire(lower))?.let {
            val on = it.groupValues[1] == "on"
            return ParsedCommand("torch", mapOf("on" to on), if (on) "Flashlight on." else "Flashlight off.")
        }
        if (TORCH_C.matches(lower)) return ParsedCommand("torch", mapOf("on" to true), "Flashlight on.")

        if (SILENT.matches(lower)) return ParsedCommand("ringer", mapOf("mode" to "silent"), "Phone on silent.")
        if (VIBRATE.matches(lower)) return ParsedCommand("ringer", mapOf("mode" to "vibrate"), "Phone on vibrate.")
        if (NORMAL.matches(lower)) return ParsedCommand("ringer", mapOf("mode" to "normal"), "Ringer on.")

        (DND_A.matchEntire(lower) ?: DND_B.matchEntire(lower))?.let {
            val on = it.groupValues[1] == "on"
            return ParsedCommand("dnd", mapOf("on" to on), if (on) "Do Not Disturb on." else "Do Not Disturb off.")
        }

        VOLUME.matchEntire(lower)?.let {
            val stream = when (it.groupValues[1]) {
                "ring", "ringer", "ringtone" -> "ring"
                "alarm" -> "alarm"
                "notification" -> "notification"
                else -> "media"
            }
            val level = it.groupValues[2].toInt().coerceIn(0, 100)
            return ParsedCommand("volume", mapOf("stream" to stream, "level" to level), "Volume $level%.")
        }
        if (VOLUME_MAX.matches(lower)) return ParsedCommand("volume", mapOf("stream" to "media", "level" to 100), "Volume 100%.")
        if (MUTE.matches(lower)) return ParsedCommand("volume", mapOf("stream" to "media", "level" to 0), "Muted.")

        BRIGHTNESS.matchEntire(lower)?.let {
            val level = it.groupValues[1].toInt().coerceIn(0, 100)
            return ParsedCommand("brightness", mapOf("level" to level), "Brightness $level%.")
        }

        (ALARM.matchEntire(lower) ?: WAKE_ME.matchEntire(lower))?.let { match ->
            val clock = parseClock(match.groupValues[1]) ?: return null
            val shown = "%02d:%02d".format(clock.first, clock.second)
            return ParsedCommand("alarm.set", mapOf("hour" to clock.first, "minute" to clock.second), "Alarm set for $shown.")
        }

        (TIMER_A.matchEntire(lower) ?: TIMER_B.matchEntire(lower))?.let { match ->
            val seconds = parseDuration(match.groupValues[1])
            if (seconds != null) {
                return ParsedCommand("timer.set", mapOf("seconds" to seconds), "Timer set for ${describeDuration(seconds)}.")
            }
        }

        for ((pattern, action) in GLOBALS) {
            if (pattern.matches(lower)) return ParsedCommand("ui.global", mapOf("action" to action.first), action.second)
        }
        for ((pattern, action) in MEDIA) {
            if (pattern.matches(lower)) return ParsedCommand("media", mapOf("action" to action.first), action.second)
        }

        SMS.matchEntire(spoken)?.let { match ->
            val target = match.groupValues[1].trim()
            val body = match.groupValues[2].trim()
            if (target.isNotEmpty() && body.isNotEmpty()) {
                val args = if (Matching.looksLikeNumber(target)) {
                    mapOf("to" to Matching.cleanNumber(target), "text" to body)
                } else {
                    mapOf("name" to target, "text" to body)
                }
                return ParsedCommand("sms.send", args, "Texting $target.")
            }
        }

        CALL.matchEntire(plain)?.let { match ->
            val target = CALL_TAIL.replace(match.groupValues[1].trim(), "").trim()
            if (target.isEmpty()) return null
            return if (Matching.looksLikeNumber(target)) {
                ParsedCommand("call.dial", mapOf("number" to Matching.cleanNumber(target)), "Calling $target.")
            } else {
                ParsedCommand("call.dial", mapOf("name" to target), "Calling $target.")
            }
        }

        OPEN.matchEntire(plain)?.let { match ->
            val target = match.groupValues[1].trim()
            if (target.isEmpty()) return null
            if (!target.contains(' ') && URL.matches(target.lowercase())) {
                val url = if (target.startsWith("http://", true) || target.startsWith("https://", true)) target else "https://$target"
                return ParsedCommand("url.open", mapOf("url" to url), "Opening $target.")
            }
            return ParsedCommand("app.open", mapOf("name" to target), "Opening $target.")
        }
        return null
    }

    private val CLOCK = Regex(
        """^(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.?|p\.m\.?)?(?:\s+(?:in the |at |this )?(morning|afternoon|evening|night|tonight))?$"""
    )

    /** "6", "6am", "6:30 pm", "18:45", "noon", "7 in the evening" -> (hour, minute) in 24-hour time. */
    fun parseClock(input: String): Pair<Int, Int>? {
        val text = input.trim().lowercase()
            .replace("o'clock", "").replace("oclock", "")
            .removeSuffix(".").trim()
        when (text) {
            "noon", "midday" -> return 12 to 0
            "midnight" -> return 0 to 0
        }
        val match = CLOCK.matchEntire(text) ?: return null
        var hour = match.groupValues[1].toInt()
        val minute = match.groupValues[2].ifEmpty { "0" }.toInt()
        val meridiem = match.groupValues[3].replace(".", "")
        val dayPart = match.groupValues[4]
        if (minute > 59) return null
        if (meridiem.isNotEmpty()) {
            if (hour !in 1..12) return null
            if (meridiem == "pm" && hour < 12) hour += 12
            if (meridiem == "am" && hour == 12) hour = 0
        } else if (dayPart.isNotEmpty()) {
            if (hour > 23) return null
            when (dayPart) {
                "afternoon", "evening" -> if (hour in 1..11) hour += 12
                "night", "tonight" -> if (hour in 5..11) hour += 12
                "morning" -> if (hour == 12) hour = 0
            }
        } else if (hour > 23) {
            return null
        }
        return hour to minute
    }

    private val DURATION_PART = Regex(
        """(\d+(?:\.\d+)?|half an?|an?|one|two|three|four|five|ten|fifteen|twenty|thirty|forty five|forty-five|sixty|ninety)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])"""
    )
    private val WORD_NUMBERS = mapOf(
        "a" to 1.0, "an" to 1.0, "one" to 1.0, "two" to 2.0, "three" to 3.0, "four" to 4.0, "five" to 5.0,
        "ten" to 10.0, "fifteen" to 15.0, "twenty" to 20.0, "thirty" to 30.0, "forty five" to 45.0,
        "forty-five" to 45.0, "sixty" to 60.0, "ninety" to 90.0, "half a" to 0.5, "half an" to 0.5,
    )

    /** "10 minutes", "1 hour 30 minutes", "90s", "half an hour", bare "10" (minutes) -> seconds. */
    fun parseDuration(input: String): Int? {
        val text = input.trim().lowercase().removeSuffix(".").trim()
        if (text.isEmpty()) return null
        text.toIntOrNull()?.let { minutes ->
            return if (minutes in 1..1440) minutes * 60 else null
        }
        var total = 0.0
        var covered = 0
        var found = false
        for (match in DURATION_PART.findAll(text)) {
            val between = text.substring(covered, match.range.first).trim()
            if (between.isNotEmpty() && between != "and" && between != ",") return null
            val amount = match.groupValues[1].toDoubleOrNull() ?: WORD_NUMBERS[match.groupValues[1]] ?: return null
            val unit = match.groupValues[2]
            total += amount * when {
                unit.startsWith("h") -> 3600.0
                unit.startsWith("m") -> 60.0
                else -> 1.0
            }
            covered = match.range.last + 1
            found = true
        }
        if (!found || text.substring(covered).trim().isNotEmpty()) return null
        val seconds = Math.round(total).toInt()
        return if (seconds in 1..86_400) seconds else null
    }

    fun describeDuration(seconds: Int): String {
        val hours = seconds / 3600
        val minutes = (seconds % 3600) / 60
        val rest = seconds % 60
        val parts = ArrayList<String>()
        if (hours > 0) parts.add("$hours h")
        if (minutes > 0) parts.add("$minutes min")
        if (rest > 0) parts.add("$rest s")
        return parts.joinToString(" ")
    }
}
