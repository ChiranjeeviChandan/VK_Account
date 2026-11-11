package ai.felix.companion.core

import java.text.Normalizer

/** Forgiving name matching for "open <app>" and "call <name>". */
object Matching {
    private val MARKS = Regex("\\p{M}+")
    private val NON_WORD = Regex("[^\\p{L}\\p{N}]+")

    /** Lower-case, accent-free, punctuation-free form used for comparisons. */
    fun fold(text: String): String {
        val decomposed = Normalizer.normalize(text, Normalizer.Form.NFKD)
        val stripped = MARKS.replace(decomposed, "")
        return NON_WORD.replace(stripped.lowercase(), " ").trim()
    }

    /**
     * 100 exact, 90 same letters ignoring spaces, 80 prefix, 70 every query
     * word starts a word of the candidate, 60 substring, 0 no match.
     */
    fun score(query: String, candidate: String): Int {
        val q = fold(query)
        val c = fold(candidate)
        if (q.isEmpty() || c.isEmpty()) return 0
        if (q == c) return 100
        if (q.replace(" ", "") == c.replace(" ", "")) return 90
        if (c.startsWith(q)) return 80
        val candidateWords = c.split(' ')
        val queryWords = q.split(' ')
        if (queryWords.all { word -> candidateWords.any { it.startsWith(word) } }) return 70
        if (c.contains(q)) return 60
        return 0
    }

    /** Result of ranking: the winning index, or the tied candidates when the match is ambiguous. */
    data class Ranked(val best: Int, val tied: List<Int>) {
        val found: Boolean get() = best >= 0
        val ambiguous: Boolean get() = tied.size > 1
    }

    fun rank(query: String, candidates: List<String>): Ranked {
        var top = 0
        val winners = ArrayList<Int>()
        for ((index, candidate) in candidates.withIndex()) {
            val value = score(query, candidate)
            if (value == 0) continue
            if (value > top) {
                top = value
                winners.clear()
                winners.add(index)
            } else if (value == top) {
                winners.add(index)
            }
        }
        if (winners.isEmpty()) return Ranked(-1, emptyList())
        // Several rows for the same person/app (same folded name) are one match, not a tie.
        val distinct = winners.distinctBy { fold(candidates[it]) }
        return Ranked(winners[0], distinct)
    }

    /** Keeps digits and a leading plus: "+91 98123-45678" -> "+919812345678". */
    fun cleanNumber(text: String): String {
        val trimmed = text.trim()
        val digits = trimmed.filter { it.isDigit() }
        return if (trimmed.startsWith("+")) "+$digits" else digits
    }

    private val NUMBER = Regex("""^\+?[\d\s().\-]{3,}$""")

    fun looksLikeNumber(text: String): Boolean {
        val trimmed = text.trim()
        return NUMBER.matches(trimmed) && trimmed.count { it.isDigit() } >= 3
    }
}
