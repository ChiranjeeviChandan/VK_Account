package ai.felix.companion

import ai.felix.companion.core.CommandLedger
import ai.felix.companion.core.Event
import ai.felix.companion.core.EventTypes
import ai.felix.companion.core.Json
import ai.felix.companion.core.QueuePolicy
import ai.felix.companion.core.QueuedRef
import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/**
 * The phone's durable state for the Core link:
 *  - `events`: captured events waiting to be accepted by Core;
 *  - `commands`: a ledger of delivered commands, so none runs twice and
 *    results survive until Core has received them.
 */
class Db(context: Context) : SQLiteOpenHelper(context.applicationContext, "felix-link.db", null, 1) {

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, " +
                "type TEXT NOT NULL, ts INTEGER NOT NULL, json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)"
        )
        db.execSQL(
            "CREATE TABLE commands (id INTEGER PRIMARY KEY, state TEXT NOT NULL, result TEXT, " +
                "acked INTEGER NOT NULL DEFAULT 0, ts INTEGER NOT NULL)"
        )
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        // Single schema version so far.
    }

    // ---- event queue ----------------------------------------------------

    /** Adds an event unless one with the same id is already queued. Returns true if it was added. */
    @Synchronized
    fun enqueue(event: Event, maxQueued: Int = QueuePolicy.DEFAULT_MAX): Boolean {
        val db = writableDatabase
        if (event.type == EventTypes.STATUS) {
            // Only the newest device status is worth sending.
            db.delete("events", "type = ?", arrayOf(EventTypes.STATUS))
        }
        val values = ContentValues()
        values.put("id", event.id)
        values.put("type", event.type)
        values.put("ts", event.ts)
        values.put("json", Json.stringify(event.toJson()))
        val row = db.insertWithOnConflict("events", null, values, SQLiteDatabase.CONFLICT_IGNORE)
        if (row == -1L) return false
        trim(db, maxQueued)
        return true
    }

    private fun trim(db: SQLiteDatabase, maxQueued: Int) {
        val count = db.compileStatement("SELECT COUNT(*) FROM events").use { it.simpleQueryForLong() }
        if (count <= maxQueued) return
        val refs = ArrayList<QueuedRef>()
        db.rawQuery("SELECT seq, type FROM events", null).use { cursor ->
            while (cursor.moveToNext()) refs.add(QueuedRef(cursor.getLong(0), cursor.getString(1)))
        }
        val drop = QueuePolicy.toDrop(refs, maxQueued)
        db.beginTransaction()
        try {
            for (seq in drop) db.delete("events", "seq = ?", arrayOf(seq.toString()))
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    /** The oldest queued events, at most [limit]. */
    @Synchronized
    fun peekEvents(limit: Int): List<Event> {
        val out = ArrayList<Event>()
        readableDatabase.rawQuery("SELECT json FROM events ORDER BY seq ASC LIMIT ?", arrayOf(limit.toString())).use { cursor ->
            while (cursor.moveToNext()) {
                Event.fromJson(Json.parseObject(cursor.getString(0)))?.let { out.add(it) }
            }
        }
        return out
    }

    @Synchronized
    fun removeEvents(ids: Collection<String>) {
        if (ids.isEmpty()) return
        val db = writableDatabase
        db.beginTransaction()
        try {
            for (id in ids) db.delete("events", "id = ?", arrayOf(id))
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    /**
     * Counts a delivery attempt that Core answered without accepting these
     * events, and drops any that it has now declined [maxAttempts] times so
     * one unacceptable event cannot block the queue forever.
     */
    @Synchronized
    fun noteRejected(ids: Collection<String>, maxAttempts: Int = 5) {
        if (ids.isEmpty()) return
        val db = writableDatabase
        db.beginTransaction()
        try {
            for (id in ids) db.execSQL("UPDATE events SET attempts = attempts + 1 WHERE id = ?", arrayOf(id))
            db.delete("events", "attempts >= ?", arrayOf(maxAttempts.toString()))
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    @Synchronized
    fun queuedEvents(): Long =
        readableDatabase.compileStatement("SELECT COUNT(*) FROM events").use { it.simpleQueryForLong() }

    // ---- command ledger -------------------------------------------------

    /**
     * Records that command [id] is about to run and says whether it should:
     * a command seen before is replayed from its stored result or refused.
     */
    @Synchronized
    fun beginCommand(id: Long, nowMs: Long): CommandLedger.Decision {
        val db = writableDatabase
        var state: String? = null
        var result: String? = null
        db.rawQuery("SELECT state, result FROM commands WHERE id = ?", arrayOf(id.toString())).use { cursor ->
            if (cursor.moveToFirst()) {
                state = cursor.getString(0)
                result = if (cursor.isNull(1)) null else cursor.getString(1)
            }
        }
        val decision = CommandLedger.decide(state, result)
        if (decision is CommandLedger.Decision.Run) {
            val values = ContentValues()
            values.put("id", id)
            values.put("state", CommandLedger.STATE_RUNNING)
            values.put("ts", nowMs)
            db.insertWithOnConflict("commands", null, values, SQLiteDatabase.CONFLICT_REPLACE)
        } else if (decision is CommandLedger.Decision.Replay) {
            // Core asked again, so it never received the result: send it once more.
            db.execSQL("UPDATE commands SET acked = 0 WHERE id = ?", arrayOf(id.toString()))
        }
        return decision
    }

    @Synchronized
    fun finishCommand(id: Long, resultJson: String, nowMs: Long) {
        val values = ContentValues()
        values.put("id", id)
        values.put("state", CommandLedger.STATE_DONE)
        values.put("result", resultJson)
        values.put("acked", 0)
        values.put("ts", nowMs)
        writableDatabase.insertWithOnConflict("commands", null, values, SQLiteDatabase.CONFLICT_REPLACE)
    }

    /** Results Core has not confirmed yet, oldest first: (command id, result JSON). */
    @Synchronized
    fun pendingResults(limit: Int): List<Pair<Long, String>> {
        val out = ArrayList<Pair<Long, String>>()
        readableDatabase.rawQuery(
            "SELECT id, result FROM commands WHERE state = ? AND acked = 0 AND result IS NOT NULL ORDER BY ts ASC LIMIT ?",
            arrayOf(CommandLedger.STATE_DONE, limit.toString())
        ).use { cursor ->
            while (cursor.moveToNext()) out.add(cursor.getLong(0) to cursor.getString(1))
        }
        return out
    }

    @Synchronized
    fun markResultsDelivered(ids: Collection<Long>) {
        if (ids.isEmpty()) return
        val db = writableDatabase
        db.beginTransaction()
        try {
            for (id in ids) db.execSQL("UPDATE commands SET acked = 1 WHERE id = ?", arrayOf(id.toString()))
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    /** Forgets delivered results older than [keepMs]; the ledger only needs to cover re-deliveries. */
    @Synchronized
    fun pruneCommands(nowMs: Long, keepMs: Long = 7L * 24 * 3600 * 1000) {
        writableDatabase.delete("commands", "acked = 1 AND ts < ?", arrayOf((nowMs - keepMs).toString()))
    }

    @Synchronized
    fun clearAll() {
        val db = writableDatabase
        db.delete("events", null, null)
        db.delete("commands", null, null)
    }
}
