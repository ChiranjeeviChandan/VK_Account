package ai.felix.companion.core

/** Error codes defined by docs/PHONE_PROTOCOL.md, plus two local ones. */
object Errors {
    const val UNSUPPORTED = "unsupported"
    const val EXPIRED = "expired"
    const val PERMISSION_DENIED = "permission_denied"
    const val NOT_FOUND = "not_found"
    const val UNAVAILABLE = "unavailable"
    const val ACCESSIBILITY_OFF = "accessibility_off"
    const val SCREEN_LOCKED = "screen_locked"
    const val TIMEOUT = "timeout"
    const val NOT_INSTALLED = "not_installed"
    const val NO_REPLY_ACTION = "no_reply_action"

    /** The command arrived without a required argument or with an unusable value. */
    const val BAD_ARGS = "bad_args"

    /** The command was understood but the platform call failed unexpectedly. */
    const val FAILED = "failed"
}

object EventTypes {
    const val MESSAGE = "message"
    const val NOTIFICATION = "notification"
    const val NOTIFICATION_REMOVED = "notification_removed"
    const val SMS = "sms"
    const val CALL = "call"
    const val STATUS = "status"
}

/** One phone -> Core event. [ts] is Unix epoch milliseconds. */
data class Event(val id: String, val type: String, val ts: Long, val data: Map<String, Any?>) {
    fun toJson(): Map<String, Any?> = linkedMapOf("id" to id, "type" to type, "ts" to ts, "data" to data)

    companion object {
        fun fromJson(json: Map<String, Any?>): Event? {
            val id = json.str("id") ?: return null
            val type = json.str("type") ?: return null
            return Event(id, type, json.long("ts") ?: 0L, json.obj("data"))
        }
    }
}

/** One Core -> phone command. [expires] is epoch milliseconds; 0 means "never". */
data class Command(val id: Long, val type: String, val args: Map<String, Any?>, val expires: Long) {
    fun isExpired(nowMs: Long): Boolean = expires > 0 && nowMs > expires

    companion object {
        fun fromJson(json: Map<String, Any?>): Command? {
            val id = json.long("id") ?: return null
            val type = json.str("type")?.trim().orEmpty()
            if (type.isEmpty()) return null
            return Command(id, type, json.obj("args"), json.long("expires") ?: 0L)
        }
    }
}

/** What a command handler produced, before it is tied to a command id. */
class Outcome private constructor(
    val ok: Boolean,
    val data: Map<String, Any?>,
    val error: String?,
    val message: String?,
) {
    companion object {
        fun ok(data: Map<String, Any?>): Outcome = Outcome(true, data, null, null)
        fun ok(vararg pairs: Pair<String, Any?>): Outcome = Outcome(true, linkedMapOf(*pairs), null, null)
        fun fail(error: String, message: String? = null): Outcome = Outcome(false, emptyMap(), error, message)
    }
}

data class CommandResult(
    val id: Long,
    val ok: Boolean,
    val data: Map<String, Any?> = emptyMap(),
    val error: String? = null,
    val message: String? = null,
) {
    fun toJson(): Map<String, Any?> {
        val out = linkedMapOf<String, Any?>("id" to id, "ok" to ok)
        if (ok) {
            out["data"] = data
        } else {
            out["error"] = error ?: Errors.FAILED
            if (!message.isNullOrEmpty()) out["message"] = message
        }
        return out
    }

    companion object {
        fun of(id: Long, outcome: Outcome): CommandResult =
            CommandResult(id, outcome.ok, outcome.data, outcome.error, outcome.message)

        fun fromJson(json: Map<String, Any?>): CommandResult? {
            val id = json.long("id") ?: return null
            return CommandResult(id, json.bool("ok"), json.obj("data"), json.str("error"), json.str("message"))
        }
    }
}

data class PairResponse(val token: String, val deviceId: String, val coreName: String, val coreVersion: String, val pollWait: Int)

data class Approval(val id: Long, val tool: String, val preview: String, val risk: String)

data class ChatReply(val reply: String, val engine: String, val online: Boolean, val approvals: List<Approval>)

/** Builds request bodies and reads responses exactly as the protocol document describes them. */
object Wire {
    const val MAX_EVENTS_PER_REQUEST = 100

    fun pairBody(code: String, deviceId: String, name: String, model: String, os: String, appVersion: String): String =
        Json.stringify(
            linkedMapOf(
                "code" to code,
                "device_id" to deviceId,
                "name" to name,
                "model" to model,
                "os" to os,
                "app_version" to appVersion,
            )
        )

    fun parsePairResponse(body: String): PairResponse? {
        val json = Json.parseObject(body)
        val token = json.str("token")?.takeIf { it.isNotEmpty() } ?: return null
        val core = json.obj("core")
        val wait = (json.int("poll_wait") ?: 25).coerceIn(1, 120)
        return PairResponse(token, json.strOr("device_id", ""), core.strOr("name", "Felix"), core.strOr("version", ""), wait)
    }

    fun eventsBody(events: List<Event>): String =
        Json.stringify(linkedMapOf("events" to events.map { it.toJson() }))

    fun parseAccepted(body: String): Set<String> = Json.parseObject(body).strings("accepted").toSet()

    fun parsePoll(body: String): List<Command> =
        Json.parseObject(body).list("commands").mapNotNull { Command.fromJson(Json.asObject(it)) }

    fun resultsBody(results: List<CommandResult>): String =
        Json.stringify(linkedMapOf("results" to results.map { it.toJson() }))

    fun chatBody(text: String, thread: String = "phone"): String =
        Json.stringify(linkedMapOf("text" to text, "thread" to thread))

    fun parseApproval(json: Map<String, Any?>): Approval? {
        val id = json.long("id") ?: return null
        return Approval(id, json.strOr("tool", ""), json.strOr("preview", ""), json.strOr("risk", ""))
    }

    fun parseApprovals(body: String): List<Approval> =
        Json.parseObject(body).list("approvals").mapNotNull { parseApproval(Json.asObject(it)) }

    fun parseChatReply(json: Map<String, Any?>): ChatReply = ChatReply(
        reply = json.strOr("reply", ""),
        engine = json.strOr("engine", ""),
        online = json.bool("online", true),
        approvals = json.list("approvals").mapNotNull { parseApproval(Json.asObject(it)) },
    )

    /** The `{"error": "..."}` code of an error response, or null. */
    fun errorCode(body: String): String? = Json.parseObject(body).str("error")
}
