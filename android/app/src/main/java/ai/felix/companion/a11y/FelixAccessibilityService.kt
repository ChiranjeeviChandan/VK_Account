package ai.felix.companion.a11y

import ai.felix.companion.core.Errors
import ai.felix.companion.core.Matching
import ai.felix.companion.core.Outcome
import ai.felix.companion.device.DeviceStatus
import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.Path
import android.graphics.Rect
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.util.ArrayDeque
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Lets Felix operate the screen the way a person would: press Back or Home,
 * tap a control, type into a field, read what is visible. It is also what
 * presses "Send" when Felix starts a new WhatsApp message.
 *
 * Every public function may be called from a background thread.
 */
class FelixAccessibilityService : AccessibilityService() {

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Felix acts on request only; it does not watch what the user does.
    }

    override fun onInterrupt() = Unit

    override fun onUnbind(intent: Intent?): Boolean {
        if (instance === this) instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        if (instance === this) instance = null
        super.onDestroy()
    }

    // ---- global actions -------------------------------------------------

    fun global(action: String): Outcome {
        val code = when (action) {
            "back" -> GLOBAL_ACTION_BACK
            "home" -> GLOBAL_ACTION_HOME
            "recents" -> GLOBAL_ACTION_RECENTS
            "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
            "quick_settings" -> GLOBAL_ACTION_QUICK_SETTINGS
            "lock" -> if (Build.VERSION.SDK_INT >= 28) GLOBAL_ACTION_LOCK_SCREEN else return tooOld("Locking the screen")
            "screenshot" -> if (Build.VERSION.SDK_INT >= 28) GLOBAL_ACTION_TAKE_SCREENSHOT else return tooOld("Taking a screenshot")
            else -> return Outcome.fail(Errors.BAD_ARGS, "Unknown action \"$action\".")
        }
        return if (performGlobalAction(code)) Outcome.ok("action" to action) else Outcome.fail(Errors.FAILED, "Android refused the \"$action\" action.")
    }

    private fun tooOld(what: String): Outcome =
        Outcome.fail(Errors.UNAVAILABLE, "$what needs Android 9 or newer.")

    // ---- finding nodes --------------------------------------------------

    private fun root(): AccessibilityNodeInfo? = try {
        rootInActiveWindow
    } catch (_: RuntimeException) {
        null
    }

    /** Breadth-first walk over the visible tree, capped so a huge screen cannot stall Felix. */
    private fun walk(start: AccessibilityNodeInfo, visit: (AccessibilityNodeInfo) -> Boolean) {
        val queue = ArrayDeque<AccessibilityNodeInfo>()
        queue.add(start)
        var seen = 0
        while (queue.isNotEmpty() && seen < MAX_NODES) {
            val node = queue.poll() ?: break
            seen++
            if (!visit(node)) return
            for (i in 0 until node.childCount) {
                val child = try {
                    node.getChild(i)
                } catch (_: RuntimeException) {
                    null
                }
                if (child != null) queue.add(child)
            }
        }
    }

    private fun find(root: AccessibilityNodeInfo, text: String?, id: String?, desc: String?): List<AccessibilityNodeInfo> {
        if (!id.isNullOrBlank()) {
            val full = if (id.contains(':')) id else "${root.packageName}:id/$id"
            return root.findAccessibilityNodeInfosByViewId(full) ?: emptyList()
        }
        if (!text.isNullOrBlank()) {
            val wanted = Matching.fold(text)
            val matches = (root.findAccessibilityNodeInfosByText(text) ?: emptyList()).filter { it.isVisibleToUser }
            val exact = matches.filter {
                Matching.fold(it.text?.toString().orEmpty()) == wanted ||
                    Matching.fold(it.contentDescription?.toString().orEmpty()) == wanted
            }
            return if (exact.isNotEmpty()) exact else matches
        }
        if (!desc.isNullOrBlank()) {
            val wanted = Matching.fold(desc)
            val out = ArrayList<AccessibilityNodeInfo>()
            walk(root) { node ->
                val label = Matching.fold(node.contentDescription?.toString().orEmpty())
                if (label.isNotEmpty() && label.contains(wanted) && node.isVisibleToUser) out.add(node)
                true
            }
            return out.sortedBy { Matching.fold(it.contentDescription?.toString().orEmpty()) != wanted }
        }
        return emptyList()
    }

    private fun clickNode(node: AccessibilityNodeInfo): Boolean {
        var current: AccessibilityNodeInfo? = node
        var hops = 0
        while (current != null && hops < 8) {
            if (current.isClickable && current.isEnabled) {
                if (current.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true
            }
            current = current.parent
            hops++
        }
        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        if (bounds.isEmpty) return false
        return tap(bounds.exactCenterX(), bounds.exactCenterY())
    }

    // ---- commands -------------------------------------------------------

    fun click(text: String?, id: String?, desc: String?, index: Int): Outcome {
        if (text.isNullOrBlank() && id.isNullOrBlank() && desc.isNullOrBlank()) {
            return Outcome.fail(Errors.BAD_ARGS, "One of text, id or desc is required.")
        }
        val root = root() ?: return Outcome.fail(Errors.NOT_FOUND, "No window is available to Felix right now.")
        val matches = find(root, text, id, desc)
        val node = matches.getOrNull(index.coerceAtLeast(0))
            ?: return Outcome.fail(Errors.NOT_FOUND, "Nothing on screen matches that.")
        return if (clickNode(node)) Outcome.ok("matches" to matches.size) else Outcome.fail(Errors.FAILED, "The control could not be pressed.")
    }

    fun type(text: String, submit: Boolean): Outcome {
        val root = root() ?: return Outcome.fail(Errors.NOT_FOUND, "No window is available to Felix right now.")
        var field = root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.takeIf { it.isEditable }
        if (field == null) {
            walk(root) { node ->
                if (node.isEditable && node.isVisibleToUser) {
                    field = node
                    false
                } else {
                    true
                }
            }
        }
        val target = field ?: return Outcome.fail(Errors.NOT_FOUND, "There is no text field on screen.")
        val arguments = Bundle()
        arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        if (!target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)) {
            return Outcome.fail(Errors.FAILED, "The text field refused the text.")
        }
        var submitted = false
        if (submit && Build.VERSION.SDK_INT >= 30) {
            submitted = target.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)
        }
        return Outcome.ok("submitted" to submitted)
    }

    fun scroll(direction: String): Outcome {
        val forward = when (direction) {
            "down", "right" -> true
            "up", "left" -> false
            else -> return Outcome.fail(Errors.BAD_ARGS, "Unknown direction \"$direction\".")
        }
        val root = root()
        if (root != null) {
            var scrollable: AccessibilityNodeInfo? = null
            walk(root) { node ->
                if (node.isScrollable && node.isVisibleToUser) {
                    scrollable = node
                    false
                } else {
                    true
                }
            }
            val action = if (forward) AccessibilityNodeInfo.ACTION_SCROLL_FORWARD else AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
            if (scrollable?.performAction(action) == true) return Outcome.ok()
        }
        // No scrollable container reported itself: drag the screen instead.
        val metrics = resources.displayMetrics
        val w = metrics.widthPixels.toFloat()
        val h = metrics.heightPixels.toFloat()
        val done = when (direction) {
            "down" -> swipeBlocking(w / 2, h * 0.72f, w / 2, h * 0.28f, 300)
            "up" -> swipeBlocking(w / 2, h * 0.28f, w / 2, h * 0.72f, 300)
            "right" -> swipeBlocking(w * 0.8f, h / 2, w * 0.2f, h / 2, 300)
            else -> swipeBlocking(w * 0.2f, h / 2, w * 0.8f, h / 2, 300)
        }
        return if (done) Outcome.ok() else Outcome.fail(Errors.FAILED, "The screen could not be scrolled.")
    }

    fun swipe(x1: Float, y1: Float, x2: Float, y2: Float, ms: Long): Outcome =
        if (swipeBlocking(x1, y1, x2, y2, ms.coerceIn(30, 5_000))) Outcome.ok() else Outcome.fail(Errors.FAILED, "The gesture was cancelled.")

    private fun tap(x: Float, y: Float): Boolean = swipeBlocking(x, y, x, y, 60)

    private fun swipeBlocking(x1: Float, y1: Float, x2: Float, y2: Float, ms: Long): Boolean {
        val path = Path()
        path.moveTo(x1.coerceAtLeast(0f), y1.coerceAtLeast(0f))
        path.lineTo(x2.coerceAtLeast(0f), y2.coerceAtLeast(0f))
        val gesture = try {
            GestureDescription.Builder().addStroke(GestureDescription.StrokeDescription(path, 0, ms)).build()
        } catch (_: IllegalArgumentException) {
            return false
        }
        val finished = CountDownLatch(1)
        val completed = AtomicBoolean(false)
        val accepted = dispatchGesture(gesture, object : GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription?) {
                completed.set(true)
                finished.countDown()
            }

            override fun onCancelled(gestureDescription: GestureDescription?) {
                finished.countDown()
            }
        }, null)
        if (!accepted) return false
        finished.await(ms + 3_000, TimeUnit.MILLISECONDS)
        return completed.get()
    }

    fun dump(): Outcome {
        val root = root() ?: return Outcome.fail(Errors.NOT_FOUND, "No window is available to Felix right now.")
        val nodes = ArrayList<Map<String, Any?>>()
        val bounds = Rect()
        walk(root) { node ->
            val text = node.text?.toString().orEmpty()
            val label = node.contentDescription?.toString().orEmpty()
            if (node.isVisibleToUser && (text.isNotEmpty() || label.isNotEmpty() || node.isClickable)) {
                node.getBoundsInScreen(bounds)
                nodes.add(
                    linkedMapOf(
                        "text" to text.take(500),
                        "desc" to label.take(200),
                        "id" to (node.viewIdResourceName ?: ""),
                        "cls" to (node.className?.toString() ?: ""),
                        "clickable" to node.isClickable,
                        "bounds" to listOf(bounds.left, bounds.top, bounds.right, bounds.bottom),
                    )
                )
            }
            nodes.size < MAX_DUMP
        }
        return Outcome.ok("package" to (root.packageName?.toString() ?: ""), "nodes" to nodes)
    }

    // ---- WhatsApp: start a new message ----------------------------------

    /**
     * Opens the chat with [number] with [text] typed in, then presses Send.
     * The screen must be on and unlocked: this drives the real WhatsApp UI.
     */
    fun sendWhatsApp(number: String, text: String, pkg: String): Outcome {
        if (pkg != "com.whatsapp" && pkg != "com.whatsapp.w4b") {
            return Outcome.fail(Errors.BAD_ARGS, "Unsupported package \"$pkg\".")
        }
        val digits = Matching.cleanNumber(number).removePrefix("+")
        if (digits.length < 6 || text.isBlank()) return Outcome.fail(Errors.BAD_ARGS, "number (E.164) and text are required.")
        if (packageManager.getLaunchIntentForPackage(pkg) == null) {
            return Outcome.fail(Errors.NOT_INSTALLED, "WhatsApp is not installed.")
        }
        if (!DeviceStatus.screenOn(this) || DeviceStatus.isLocked(this)) {
            return Outcome.fail(Errors.SCREEN_LOCKED, "Unlock the phone: starting a WhatsApp message needs the screen.")
        }
        val link = Uri.parse("https://wa.me/$digits?text=" + Uri.encode(text))
        val open = Intent(Intent.ACTION_VIEW, link)
            .setPackage(pkg)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        try {
            startActivity(open)
        } catch (_: ActivityNotFoundException) {
            return Outcome.fail(Errors.NOT_INSTALLED, "WhatsApp could not open that chat.")
        } catch (e: SecurityException) {
            return Outcome.fail(Errors.FAILED, e.message)
        }

        val deadline = SystemClock.uptimeMillis() + SEND_TIMEOUT_MS
        while (SystemClock.uptimeMillis() < deadline) {
            SystemClock.sleep(300)
            if (DeviceStatus.isLocked(this)) return Outcome.fail(Errors.SCREEN_LOCKED, "The phone was locked before the message was sent.")
            val root = root() ?: continue
            if (root.packageName?.toString() != pkg) continue
            val refusal = notOnWhatsApp(root)
            if (refusal != null) {
                performGlobalAction(GLOBAL_ACTION_BACK)
                return Outcome.fail(Errors.NOT_FOUND, refusal)
            }
            val button = sendButton(root, pkg) ?: continue
            if (!button.isEnabled) continue
            if (!clickNode(button)) continue
            SystemClock.sleep(700)
            // Leave the chat so the phone is not left sitting inside WhatsApp.
            performGlobalAction(GLOBAL_ACTION_BACK)
            SystemClock.sleep(250)
            performGlobalAction(GLOBAL_ACTION_BACK)
            return Outcome.ok()
        }
        return Outcome.fail(Errors.TIMEOUT, "WhatsApp did not show a Send button in time.")
    }

    private fun sendButton(root: AccessibilityNodeInfo, pkg: String): AccessibilityNodeInfo? {
        val byId = root.findAccessibilityNodeInfosByViewId("$pkg:id/send")
        byId?.firstOrNull { it.isVisibleToUser }?.let { return it }
        var found: AccessibilityNodeInfo? = null
        walk(root) { node ->
            val label = node.contentDescription?.toString()?.trim().orEmpty()
            if (node.isClickable && node.isVisibleToUser && label.equals("Send", ignoreCase = true)) {
                found = node
                false
            } else {
                true
            }
        }
        return found
    }

    private fun notOnWhatsApp(root: AccessibilityNodeInfo): String? {
        for (phrase in listOf("isn't on WhatsApp", "is not on WhatsApp", "not on WhatsApp")) {
            val hits = root.findAccessibilityNodeInfosByText(phrase)
            if (!hits.isNullOrEmpty()) return "That number is not on WhatsApp."
        }
        return null
    }

    companion object {
        private const val MAX_NODES = 4_000
        private const val MAX_DUMP = 400
        private const val SEND_TIMEOUT_MS = 15_000L

        /** The running service, or null when the user has not enabled it. */
        @Volatile
        var instance: FelixAccessibilityService? = null
            private set
    }
}
