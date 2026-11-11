package ai.felix.companion.device

import ai.felix.companion.core.Errors
import ai.felix.companion.core.Matching
import ai.felix.companion.core.Outcome
import android.content.Context
import android.net.Uri
import android.provider.ContactsContract
import android.provider.ContactsContract.CommonDataKinds.Email
import android.provider.ContactsContract.CommonDataKinds.Phone

/** Read-only access to the phone's address book. */
object ContactsBridge {
    class Contact(val name: String) {
        val phones = ArrayList<String>()
        val emails = ArrayList<String>()

        fun toJson(): Map<String, Any?> = linkedMapOf("name" to name, "phones" to phones, "emails" to emails)

        fun addPhone(number: String?) {
            val value = number?.trim().orEmpty()
            if (value.isEmpty()) return
            val digits = Matching.cleanNumber(value)
            if (phones.none { Matching.cleanNumber(it) == digits }) phones.add(value)
        }

        fun addEmail(address: String?) {
            val value = address?.trim().orEmpty()
            if (value.isNotEmpty() && emails.none { it.equals(value, ignoreCase = true) }) emails.add(value)
        }
    }

    fun search(context: Context, query: String, limit: Int): Outcome {
        if (!Permissions.contacts(context)) return denied()
        if (query.isBlank()) return Outcome.fail(Errors.BAD_ARGS, "A name or number to search for is required.")
        return try {
            Outcome.ok("contacts" to find(context, query, limit.coerceIn(1, 100)).map { it.toJson() })
        } catch (e: SecurityException) {
            denied()
        } catch (e: RuntimeException) {
            Outcome.fail(Errors.FAILED, e.message)
        }
    }

    fun dump(context: Context, limit: Int): Outcome {
        if (!Permissions.contacts(context)) return denied()
        return try {
            Outcome.ok("contacts" to all(context, limit.coerceIn(1, 20_000)).map { it.toJson() })
        } catch (e: SecurityException) {
            denied()
        } catch (e: RuntimeException) {
            Outcome.fail(Errors.FAILED, e.message)
        }
    }

    private fun denied(): Outcome = Outcome.fail(Errors.PERMISSION_DENIED, "Felix is not allowed to read contacts.")

    /** Contacts matching a name or phone number. Requires READ_CONTACTS. */
    fun find(context: Context, query: String, limit: Int): List<Contact> {
        val resolver = context.contentResolver
        val byId = LinkedHashMap<Long, Contact>()

        if (Matching.looksLikeNumber(query)) {
            val uri = Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI, Uri.encode(query.trim()))
            resolver.query(
                uri, arrayOf(ContactsContract.PhoneLookup._ID, ContactsContract.PhoneLookup.DISPLAY_NAME), null, null, null
            )?.use { cursor ->
                while (cursor.moveToNext() && byId.size < limit) {
                    val name = cursor.getString(1) ?: continue
                    byId.getOrPut(cursor.getLong(0)) { Contact(name) }
                }
            }
        }

        val filter = Uri.withAppendedPath(ContactsContract.Contacts.CONTENT_FILTER_URI, Uri.encode(query.trim()))
        resolver.query(
            filter,
            arrayOf(ContactsContract.Contacts._ID, ContactsContract.Contacts.DISPLAY_NAME_PRIMARY),
            null, null, null
        )?.use { cursor ->
            while (cursor.moveToNext() && byId.size < limit) {
                val name = cursor.getString(1) ?: continue
                byId.getOrPut(cursor.getLong(0)) { Contact(name) }
            }
        }
        if (byId.isEmpty()) return emptyList()

        val ids = byId.keys.toList()
        val placeholders = ids.joinToString(",") { "?" }
        val args = ids.map { it.toString() }.toTypedArray()
        resolver.query(
            Phone.CONTENT_URI,
            arrayOf(Phone.CONTACT_ID, Phone.NUMBER, Phone.NORMALIZED_NUMBER),
            "${Phone.CONTACT_ID} IN ($placeholders)", args, null
        )?.use { cursor ->
            while (cursor.moveToNext()) {
                val contact = byId[cursor.getLong(0)] ?: continue
                contact.addPhone(cursor.getString(2) ?: cursor.getString(1))
            }
        }
        resolver.query(
            Email.CONTENT_URI,
            arrayOf(Email.CONTACT_ID, Email.ADDRESS),
            "${Email.CONTACT_ID} IN ($placeholders)", args, null
        )?.use { cursor ->
            while (cursor.moveToNext()) {
                byId[cursor.getLong(0)]?.addEmail(cursor.getString(1))
            }
        }
        return byId.values.toList()
    }

    /** Every contact that has a phone number or an e-mail address. */
    fun all(context: Context, limit: Int): List<Contact> {
        val resolver = context.contentResolver
        val byId = LinkedHashMap<Long, Contact>()
        resolver.query(
            Phone.CONTENT_URI,
            arrayOf(Phone.CONTACT_ID, Phone.DISPLAY_NAME_PRIMARY, Phone.NUMBER, Phone.NORMALIZED_NUMBER),
            null, null, "${Phone.DISPLAY_NAME_PRIMARY} COLLATE NOCASE ASC"
        )?.use { cursor ->
            while (cursor.moveToNext()) {
                val id = cursor.getLong(0)
                val existing = byId[id]
                if (existing == null && byId.size >= limit) continue
                val contact = existing ?: Contact(cursor.getString(1) ?: continue).also { byId[id] = it }
                contact.addPhone(cursor.getString(3) ?: cursor.getString(2))
            }
        }
        resolver.query(
            Email.CONTENT_URI,
            arrayOf(Email.CONTACT_ID, Email.DISPLAY_NAME_PRIMARY, Email.ADDRESS),
            null, null, "${Email.DISPLAY_NAME_PRIMARY} COLLATE NOCASE ASC"
        )?.use { cursor ->
            while (cursor.moveToNext()) {
                val id = cursor.getLong(0)
                val existing = byId[id]
                if (existing == null && byId.size >= limit) continue
                val contact = existing ?: Contact(cursor.getString(1) ?: continue).also { byId[id] = it }
                contact.addEmail(cursor.getString(2))
            }
        }
        return byId.values.toList()
    }

    /** Outcome of turning a spoken name into one phone number. */
    sealed class Resolution {
        data class Found(val name: String, val number: String) : Resolution()
        data class Ambiguous(val names: List<String>) : Resolution()
        data class Missing(val reason: String) : Resolution()
    }

    /** Resolves "Ravi" to a single number, refusing to guess between several people. */
    fun resolveNumber(context: Context, name: String): Resolution {
        if (!Permissions.contacts(context)) {
            return Resolution.Missing("Felix is not allowed to read contacts, so it cannot look up \"$name\".")
        }
        val matches = try {
            find(context, name, 25).filter { it.phones.isNotEmpty() }
        } catch (_: SecurityException) {
            return Resolution.Missing("Felix is not allowed to read contacts.")
        } catch (e: RuntimeException) {
            return Resolution.Missing("Contacts could not be read (${e.message}).")
        }
        if (matches.isEmpty()) return Resolution.Missing("No contact with a phone number matches \"$name\".")
        val ranked = Matching.rank(name, matches.map { it.name })
        if (!ranked.found) return Resolution.Missing("No contact with a phone number matches \"$name\".")
        if (ranked.ambiguous) return Resolution.Ambiguous(ranked.tied.map { matches[it].name })
        val chosen = matches[ranked.best]
        return Resolution.Found(chosen.name, chosen.phones.first())
    }
}
