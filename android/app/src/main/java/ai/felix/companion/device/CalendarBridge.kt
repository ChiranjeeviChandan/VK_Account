package ai.felix.companion.device

import ai.felix.companion.core.Errors
import ai.felix.companion.core.Outcome
import ai.felix.companion.core.bool
import ai.felix.companion.core.list
import ai.felix.companion.core.long
import ai.felix.companion.core.str
import ai.felix.companion.core.strings
import android.Manifest
import android.content.ContentResolver
import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.provider.CalendarContract
import android.provider.CalendarContract.Attendees
import android.provider.CalendarContract.Calendars
import android.provider.CalendarContract.Events
import android.provider.CalendarContract.Instances
import android.provider.CalendarContract.Reminders
import java.time.DateTimeException
import java.time.Instant
import java.time.ZoneId
import java.time.ZoneOffset

/**
 * The calendars already synced to this phone (Google, Exchange, ...), exposed
 * to Felix Core. Writes go through the system calendar provider, so the
 * phone's own sync delivers them to the account they belong to.
 *
 * All-day events: Android stores them as UTC midnights. Towards Core they are
 * reported as midnights in the phone's own time zone (named in `tz`), so that
 * an instant read in the owner's zone lands on the right calendar day. From
 * Core either form is accepted: a UTC midnight, or any instant on the wanted
 * day as read in the event's `tz`.
 */
object CalendarBridge {
    private const val DAY_MS = 86_400_000L
    private const val MAX_EVENTS = 1_000

    private fun canRead(context: Context) = Permissions.has(context, Manifest.permission.READ_CALENDAR)
    private fun canWrite(context: Context) = Permissions.has(context, Manifest.permission.WRITE_CALENDAR)
    private fun denied() = Outcome.fail(Errors.PERMISSION_DENIED, "Felix is not allowed to use the calendar.")

    private fun guarded(block: () -> Outcome): Outcome = try {
        block()
    } catch (_: SecurityException) {
        denied()
    } catch (e: RuntimeException) {
        Outcome.fail(Errors.FAILED, e.message ?: e.javaClass.simpleName)
    }

    fun calendars(context: Context): Outcome {
        if (!canRead(context)) return denied()
        return guarded {
            val out = ArrayList<Map<String, Any?>>()
            context.contentResolver.query(
                Calendars.CONTENT_URI,
                arrayOf(
                    Calendars._ID, Calendars.CALENDAR_DISPLAY_NAME, Calendars.ACCOUNT_NAME,
                    Calendars.IS_PRIMARY, Calendars.CALENDAR_ACCESS_LEVEL,
                ),
                null, null, "${Calendars.IS_PRIMARY} DESC, ${Calendars._ID} ASC"
            )?.use { cursor ->
                while (cursor.moveToNext()) {
                    out.add(
                        linkedMapOf(
                            "id" to cursor.getLong(0).toString(),
                            "name" to (cursor.getString(1) ?: ""),
                            "account" to (cursor.getString(2) ?: ""),
                            "primary" to (cursor.getInt(3) == 1),
                            "writable" to (cursor.getInt(4) >= Calendars.CAL_ACCESS_CONTRIBUTOR),
                        )
                    )
                }
            }
            Outcome.ok("calendars" to out)
        }
    }

    fun list(context: Context, startMs: Long, endMs: Long): Outcome {
        if (!canRead(context)) return denied()
        if (endMs <= startMs) return Outcome.fail(Errors.BAD_ARGS, "end_ms must be after start_ms.")
        return guarded {
            val resolver = context.contentResolver
            val uri = Instances.CONTENT_URI.buildUpon().also {
                ContentUris.appendId(it, startMs)
                ContentUris.appendId(it, endMs)
            }.build()
            val events = ArrayList<LinkedHashMap<String, Any?>>()
            val eventIds = LinkedHashSet<Long>()
            val deviceZone = ZoneId.systemDefault()
            resolver.query(
                uri,
                arrayOf(
                    Instances.EVENT_ID, Instances.BEGIN, Instances.END, Instances.TITLE, Instances.ALL_DAY,
                    Instances.EVENT_LOCATION, Instances.DESCRIPTION, Instances.CALENDAR_DISPLAY_NAME,
                    Instances.CALENDAR_ID, Instances.ORGANIZER, Instances.EVENT_TIMEZONE,
                ),
                "${Instances.VISIBLE} = 1", null, "${Instances.BEGIN} ASC"
            )?.use { cursor ->
                while (cursor.moveToNext() && events.size < MAX_EVENTS) {
                    val eventId = cursor.getLong(0)
                    val allDay = cursor.getInt(4) == 1
                    val begin = if (allDay) localMidnight(cursor.getLong(1), deviceZone) else cursor.getLong(1)
                    val finish = if (allDay) localMidnight(cursor.getLong(2), deviceZone) else cursor.getLong(2)
                    eventIds.add(eventId)
                    events.add(
                        linkedMapOf(
                            "id" to eventId.toString(),
                            "instance_start_ms" to begin,
                            "title" to (cursor.getString(3) ?: ""),
                            "start_ms" to begin,
                            "end_ms" to finish,
                            "all_day" to allDay,
                            "location" to (cursor.getString(5) ?: ""),
                            "description" to (cursor.getString(6) ?: ""),
                            "calendar" to (cursor.getString(7) ?: ""),
                            "calendar_id" to cursor.getLong(8).toString(),
                            "organizer" to (cursor.getString(9) ?: ""),
                            "attendees" to ArrayList<Map<String, Any?>>(),
                            "tz" to (if (allDay) deviceZone.id else (cursor.getString(10) ?: deviceZone.id)),
                        )
                    )
                }
            }
            val attendees = attendeesFor(resolver, eventIds)
            for (event in events) {
                val id = (event["id"] as String).toLong()
                event["attendees"] = attendees[id] ?: emptyList<Map<String, Any?>>()
            }
            Outcome.ok("events" to events)
        }
    }

    private fun attendeesFor(resolver: ContentResolver, eventIds: Collection<Long>): Map<Long, List<Map<String, Any?>>> {
        val out = HashMap<Long, ArrayList<Map<String, Any?>>>()
        for (chunk in eventIds.chunked(200)) {
            val placeholders = chunk.joinToString(",") { "?" }
            resolver.query(
                Attendees.CONTENT_URI,
                arrayOf(Attendees.EVENT_ID, Attendees.ATTENDEE_NAME, Attendees.ATTENDEE_EMAIL, Attendees.ATTENDEE_STATUS),
                "${Attendees.EVENT_ID} IN ($placeholders)",
                chunk.map { it.toString() }.toTypedArray(),
                null
            )?.use { cursor ->
                while (cursor.moveToNext()) {
                    val status = when (cursor.getInt(3)) {
                        Attendees.ATTENDEE_STATUS_ACCEPTED -> "accepted"
                        Attendees.ATTENDEE_STATUS_DECLINED -> "declined"
                        Attendees.ATTENDEE_STATUS_TENTATIVE -> "tentative"
                        Attendees.ATTENDEE_STATUS_INVITED -> "invited"
                        else -> "none"
                    }
                    out.getOrPut(cursor.getLong(0)) { ArrayList() }.add(
                        linkedMapOf(
                            "name" to (cursor.getString(1) ?: ""),
                            "email" to (cursor.getString(2) ?: ""),
                            "status" to status,
                        )
                    )
                }
            }
        }
        return out
    }

    /** The calendar new events go to: the given one, else the primary writable one. */
    private fun pickCalendar(resolver: ContentResolver, requested: Long?): Long? {
        var chosen: Long? = null
        resolver.query(
            Calendars.CONTENT_URI,
            arrayOf(Calendars._ID, Calendars.CALENDAR_ACCESS_LEVEL),
            "${Calendars.CALENDAR_ACCESS_LEVEL} >= ?",
            arrayOf(Calendars.CAL_ACCESS_CONTRIBUTOR.toString()),
            "${Calendars.IS_PRIMARY} DESC, ${Calendars.VISIBLE} DESC, ${Calendars._ID} ASC"
        )?.use { cursor ->
            while (cursor.moveToNext()) {
                val id = cursor.getLong(0)
                if (requested != null) {
                    if (id == requested) {
                        chosen = id
                        break
                    }
                } else {
                    chosen = id
                    break
                }
            }
        }
        return chosen
    }

    private fun zoneOf(tz: String?): ZoneId = try {
        if (tz.isNullOrBlank()) ZoneId.systemDefault() else ZoneId.of(tz)
    } catch (_: DateTimeException) {
        ZoneId.systemDefault()
    }

    /** Midnight in [zone] of the calendar day that the provider stored as the UTC midnight [utcMidnightMs]. */
    private fun localMidnight(utcMidnightMs: Long, zone: ZoneId): Long =
        Instant.ofEpochMilli(utcMidnightMs).atZone(ZoneOffset.UTC).toLocalDate().atStartOfDay(zone).toInstant().toEpochMilli()

    /** UTC midnight of the calendar day that [ms] falls on (read in [zone] unless it already is a UTC midnight). */
    private fun utcMidnight(ms: Long, zone: ZoneId): Long {
        if (Math.floorMod(ms, DAY_MS) == 0L) return ms
        return Instant.ofEpochMilli(ms).atZone(zone).toLocalDate().atStartOfDay(ZoneOffset.UTC).toInstant().toEpochMilli()
    }

    fun create(context: Context, args: Map<String, Any?>): Outcome {
        if (!canWrite(context) || !canRead(context)) return denied()
        val title = args.str("title")?.trim().orEmpty()
        val start = args.long("start_ms")
        val end = args.long("end_ms")
        if (title.isEmpty() || start == null || end == null) {
            return Outcome.fail(Errors.BAD_ARGS, "title, start_ms and end_ms are required.")
        }
        return guarded {
            val resolver = context.contentResolver
            val requested = args.str("calendar_id")?.toLongOrNull()
            val calendarId = pickCalendar(resolver, requested)
                ?: return@guarded Outcome.fail(
                    Errors.NOT_FOUND,
                    if (requested != null) "That calendar does not exist or is read-only." else "This phone has no writable calendar."
                )
            val allDay = args.bool("all_day")
            val zone = zoneOf(args.str("tz"))
            val values = ContentValues()
            values.put(Events.CALENDAR_ID, calendarId)
            values.put(Events.TITLE, title)
            if (allDay) {
                val first = utcMidnight(start, zone)
                var last = utcMidnight(end, zone)
                if (last <= first) last = first + DAY_MS
                values.put(Events.DTSTART, first)
                values.put(Events.DTEND, last)
                values.put(Events.ALL_DAY, 1)
                values.put(Events.EVENT_TIMEZONE, "UTC")
            } else {
                if (end <= start) return@guarded Outcome.fail(Errors.BAD_ARGS, "end_ms must be after start_ms.")
                values.put(Events.DTSTART, start)
                values.put(Events.DTEND, end)
                values.put(Events.ALL_DAY, 0)
                values.put(Events.EVENT_TIMEZONE, zone.id)
            }
            args.str("location")?.let { values.put(Events.EVENT_LOCATION, it) }
            args.str("description")?.let { values.put(Events.DESCRIPTION, it) }
            val created = resolver.insert(Events.CONTENT_URI, values)
                ?: return@guarded Outcome.fail(Errors.FAILED, "The calendar refused the event.")
            val eventId = ContentUris.parseId(created)
            writeAttendees(resolver, eventId, args.strings("attendees"), replace = false)
            writeReminders(resolver, eventId, args.list("reminders"), replace = false)
            Outcome.ok("id" to eventId.toString())
        }
    }

    fun update(context: Context, args: Map<String, Any?>): Outcome {
        if (!canWrite(context) || !canRead(context)) return denied()
        val eventId = args.str("id")?.toLongOrNull() ?: return Outcome.fail(Errors.BAD_ARGS, "id is required.")
        return guarded {
            val resolver = context.contentResolver
            val uri = ContentUris.withAppendedId(Events.CONTENT_URI, eventId)
            var found = false
            var oldStart = 0L
            var oldEnd: Long? = null
            var recurring = false
            var wasAllDay = false
            var oldZone: String? = null
            resolver.query(
                uri, arrayOf(Events.DTSTART, Events.DTEND, Events.RRULE, Events.ALL_DAY, Events.EVENT_TIMEZONE),
                null, null, null
            )?.use { cursor ->
                if (cursor.moveToFirst()) {
                    found = true
                    oldStart = cursor.getLong(0)
                    oldEnd = if (cursor.isNull(1)) null else cursor.getLong(1)
                    recurring = !cursor.getString(2).isNullOrEmpty()
                    wasAllDay = cursor.getInt(3) == 1
                    oldZone = cursor.getString(4)
                }
            }
            if (!found) return@guarded Outcome.fail(Errors.NOT_FOUND, "No such event.")

            val values = ContentValues()
            args.str("title")?.let { values.put(Events.TITLE, it) }
            args.str("location")?.let { values.put(Events.EVENT_LOCATION, it) }
            args.str("description")?.let { values.put(Events.DESCRIPTION, it) }

            val allDay = if (args.containsKey("all_day")) args.bool("all_day") else wasAllDay
            val zone = zoneOf(args.str("tz") ?: oldZone)
            val newStart = args.long("start_ms")
            val newEnd = args.long("end_ms")
            val timingChanged = newStart != null || newEnd != null || args.containsKey("all_day") || args.containsKey("tz")
            if (timingChanged) {
                var start = newStart ?: oldStart
                // Moving only the start keeps the length of a one-off event.
                var end = newEnd ?: oldEnd?.let { it + (start - oldStart) }
                if (allDay) {
                    start = utcMidnight(start, zone)
                    end = end?.let { utcMidnight(it, zone) }
                    if (end != null && end <= start) end = start + DAY_MS
                } else if (end != null && end <= start) {
                    return@guarded Outcome.fail(Errors.BAD_ARGS, "end_ms must be after start_ms.")
                }
                values.put(Events.DTSTART, start)
                values.put(Events.ALL_DAY, if (allDay) 1 else 0)
                values.put(Events.EVENT_TIMEZONE, if (allDay) "UTC" else zone.id)
                if (recurring) {
                    // Recurring events carry a duration instead of an end time.
                    if (newEnd != null && end != null) {
                        values.put(Events.DURATION, if (allDay) "P${(end - start) / DAY_MS}D" else "P${(end - start) / 1000}S")
                    }
                } else if (end != null) {
                    values.put(Events.DTEND, end)
                }
            }
            if (values.size() > 0) {
                val rows = resolver.update(uri, values, null, null)
                if (rows == 0) return@guarded Outcome.fail(Errors.NOT_FOUND, "No such event.")
            }
            if (args.containsKey("attendees")) writeAttendees(resolver, eventId, args.strings("attendees"), replace = true)
            if (args.containsKey("reminders")) writeReminders(resolver, eventId, args.list("reminders"), replace = true)
            Outcome.ok("id" to eventId.toString())
        }
    }

    fun delete(context: Context, id: String?): Outcome {
        if (!canWrite(context)) return denied()
        val eventId = id?.toLongOrNull() ?: return Outcome.fail(Errors.BAD_ARGS, "id is required.")
        return guarded {
            val rows = context.contentResolver.delete(ContentUris.withAppendedId(Events.CONTENT_URI, eventId), null, null)
            if (rows == 0) Outcome.fail(Errors.NOT_FOUND, "No such event.") else Outcome.ok()
        }
    }

    private fun writeAttendees(resolver: ContentResolver, eventId: Long, emails: List<String>, replace: Boolean) {
        if (replace) {
            resolver.delete(
                Attendees.CONTENT_URI,
                "${Attendees.EVENT_ID} = ? AND ${Attendees.ATTENDEE_RELATIONSHIP} != ?",
                arrayOf(eventId.toString(), Attendees.RELATIONSHIP_ORGANIZER.toString())
            )
        }
        for (email in emails.map { it.trim() }.filter { it.contains('@') }.distinct()) {
            val values = ContentValues()
            values.put(Attendees.EVENT_ID, eventId)
            values.put(Attendees.ATTENDEE_EMAIL, email)
            values.put(Attendees.ATTENDEE_NAME, "")
            values.put(Attendees.ATTENDEE_RELATIONSHIP, Attendees.RELATIONSHIP_ATTENDEE)
            values.put(Attendees.ATTENDEE_TYPE, Attendees.TYPE_REQUIRED)
            values.put(Attendees.ATTENDEE_STATUS, Attendees.ATTENDEE_STATUS_INVITED)
            resolver.insert(Attendees.CONTENT_URI, values)
        }
    }

    private fun writeReminders(resolver: ContentResolver, eventId: Long, minutes: List<Any?>, replace: Boolean) {
        if (replace) {
            resolver.delete(Reminders.CONTENT_URI, "${Reminders.EVENT_ID} = ?", arrayOf(eventId.toString()))
        }
        val wanted = minutes.mapNotNull { (it as? Number)?.toInt() ?: (it as? String)?.trim()?.toIntOrNull() }
            .filter { it in 0..40_320 }
            .distinct()
        for (value in wanted) {
            val values = ContentValues()
            values.put(Reminders.EVENT_ID, eventId)
            values.put(Reminders.MINUTES, value)
            values.put(Reminders.METHOD, Reminders.METHOD_ALERT)
            resolver.insert(Reminders.CONTENT_URI, values)
        }
    }
}
