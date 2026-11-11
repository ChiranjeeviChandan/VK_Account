package ai.felix.companion.overlay

import ai.felix.companion.Hub
import ai.felix.companion.R
import ai.felix.companion.core.Approval
import ai.felix.companion.core.ChatReply
import ai.felix.companion.core.Json
import ai.felix.companion.core.OfflineCommands
import ai.felix.companion.core.SseParser
import ai.felix.companion.core.Wire
import ai.felix.companion.core.str
import ai.felix.companion.core.strOr
import ai.felix.companion.device.DeviceStatus
import ai.felix.companion.device.Permissions
import ai.felix.companion.device.Speaker
import ai.felix.companion.link.CommandExecutor
import ai.felix.companion.net.CoreClient
import ai.felix.companion.net.CoreUnreachable
import ai.felix.companion.ui.FelixMark
import ai.felix.companion.ui.Palette
import ai.felix.companion.ui.Ui
import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.graphics.PixelFormat
import android.os.Build
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.text.InputType
import android.view.ContextThemeWrapper
import android.view.Gravity
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.ViewGroup
import android.view.WindowManager
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import java.io.IOException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import kotlin.math.abs

/**
 * The floating Felix bubble and the chat panel it opens, drawn over whatever
 * app is in front. Typed or spoken requests go to Felix Core; when Core is out
 * of reach, phone actions are handled on the spot by the offline parser.
 *
 * All view work happens on the main thread; network and device work happens
 * on [worker].
 */
class OverlayController(
    private val service: Context,
    private val client: CoreClient,
    private val executor: CommandExecutor,
) {
    private val context: Context = ContextThemeWrapper(service, R.style.Theme_Felix)
    private val windows: WindowManager? = service.getSystemService(WindowManager::class.java)
    private val prefs = Hub.prefs(service)
    private val worker: ExecutorService = Executors.newSingleThreadExecutor { Thread(it, "felix-overlay") }
    private var palette = Palette.of(service)

    private var bubble: View? = null
    private var bubbleParams: WindowManager.LayoutParams? = null
    private var panel: View? = null
    private var messageList: LinearLayout? = null
    private var messageScroll: ScrollView? = null
    private var approvalsBox: LinearLayout? = null
    private var input: EditText? = null
    private var statusLabel: TextView? = null
    private var micButton: Button? = null
    private var sendButton: Button? = null

    private var recognizer: SpeechRecognizer? = null
    private var listening = false
    private var busy = false
    private var destroyed = false

    /** One message of the conversation; [view] is its bubble while the panel is open. */
    private class Line(val fromUser: Boolean, var text: String) {
        var view: TextView? = null
    }

    /** Kept so the conversation is still there when the panel is reopened. */
    private val transcript = ArrayList<Line>()

    private val linkListener: () -> Unit = { renderStatus() }

    // ---- bubble ---------------------------------------------------------

    fun show() {
        if (bubble != null || destroyed) return
        val manager = windows ?: return
        val size = Ui.dp(context, 52)
        val view = View(context)
        view.background = FelixMark(palette.accent, palette.onAccent)
        view.contentDescription = context.getString(R.string.overlay_bubble_description)
        view.elevation = Ui.dp(context, 6).toFloat()
        val metrics = context.resources.displayMetrics
        val params = WindowManager.LayoutParams(
            size, size,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT
        )
        params.gravity = Gravity.TOP or Gravity.START
        params.x = if (prefs.bubbleX >= 0) prefs.bubbleX else metrics.widthPixels - size - Ui.dp(context, 8)
        params.y = if (prefs.bubbleY >= 0) prefs.bubbleY else (metrics.heightPixels * 0.35f).toInt()
        attachDrag(view, params)
        try {
            manager.addView(view, params)
        } catch (_: RuntimeException) {
            return // the overlay permission was withdrawn
        }
        bubble = view
        bubbleParams = params
        Hub.addListener(linkListener)
    }

    @SuppressLint("ClickableViewAccessibility")
    private fun attachDrag(view: View, params: WindowManager.LayoutParams) {
        val slop = ViewConfiguration.get(context).scaledTouchSlop
        var downX = 0f
        var downY = 0f
        var startX = 0
        var startY = 0
        var dragging = false
        view.setOnClickListener { togglePanel() }
        view.setOnTouchListener { touched, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = event.rawX
                    downY = event.rawY
                    startX = params.x
                    startY = params.y
                    dragging = false
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = event.rawX - downX
                    val dy = event.rawY - downY
                    if (!dragging && (abs(dx) > slop || abs(dy) > slop)) dragging = true
                    if (dragging) {
                        params.x = (startX + dx).toInt()
                        params.y = (startY + dy).toInt()
                        updateBubble(params)
                    }
                    true
                }
                MotionEvent.ACTION_UP -> {
                    if (dragging) {
                        snapToEdge(params)
                    } else {
                        touched.performClick()
                    }
                    true
                }
                MotionEvent.ACTION_CANCEL -> {
                    if (dragging) snapToEdge(params)
                    true
                }
                else -> false
            }
        }
    }

    private fun snapToEdge(params: WindowManager.LayoutParams) {
        val metrics = context.resources.displayMetrics
        val size = Ui.dp(context, 52)
        val margin = Ui.dp(context, 8)
        params.x = if (params.x + size / 2 < metrics.widthPixels / 2) margin else metrics.widthPixels - size - margin
        params.y = params.y.coerceIn(margin, (metrics.heightPixels - size - margin).coerceAtLeast(margin))
        updateBubble(params)
        prefs.bubbleX = params.x
        prefs.bubbleY = params.y
    }

    private fun updateBubble(params: WindowManager.LayoutParams) {
        val view = bubble ?: return
        try {
            windows?.updateViewLayout(view, params)
        } catch (_: RuntimeException) {
        }
    }

    fun destroy() {
        destroyed = true
        Hub.removeListener(linkListener)
        hidePanel()
        bubble?.let { removeWindow(it) }
        bubble = null
        stopListening()
        recognizer?.destroy()
        recognizer = null
        worker.shutdownNow()
    }

    private fun removeWindow(view: View) {
        try {
            windows?.removeView(view)
        } catch (_: RuntimeException) {
            // Already gone.
        }
    }

    // ---- panel ----------------------------------------------------------

    private inner class PanelRoot(context: Context) : LinearLayout(context) {
        override fun dispatchKeyEvent(event: KeyEvent): Boolean {
            if (event.keyCode == KeyEvent.KEYCODE_BACK) {
                if (event.action == KeyEvent.ACTION_UP) hidePanel()
                return true
            }
            return super.dispatchKeyEvent(event)
        }

        @SuppressLint("ClickableViewAccessibility")
        override fun onTouchEvent(event: MotionEvent): Boolean {
            if (event.actionMasked == MotionEvent.ACTION_OUTSIDE) {
                hidePanel()
                return true
            }
            return super.onTouchEvent(event)
        }
    }

    private fun togglePanel() {
        if (panel == null) showPanel() else hidePanel()
    }

    private fun showPanel() {
        if (panel != null || destroyed) return
        val manager = windows ?: return
        palette = Palette.of(service)
        val metrics = context.resources.displayMetrics
        val pad = Ui.dp(context, 14)

        val root = PanelRoot(context)
        root.orientation = LinearLayout.VERTICAL
        root.background = Ui.rounded(palette.card, Ui.dp(context, 18).toFloat(), palette.divider, Ui.dp(context, 1))
        root.setPadding(pad, pad, pad, pad)
        root.elevation = Ui.dp(context, 10).toFloat()
        root.isFocusableInTouchMode = true

        // Header: title, link status, read-aloud switch, close.
        val header = Ui.row(context)
        val mark = View(context)
        mark.background = FelixMark(palette.accent, palette.onAccent)
        val markParams = LinearLayout.LayoutParams(Ui.dp(context, 28), Ui.dp(context, 28))
        markParams.rightMargin = Ui.dp(context, 10)
        header.addView(mark, markParams)
        val titles = LinearLayout(context)
        titles.orientation = LinearLayout.VERTICAL
        titles.addView(Ui.label(context, context.getString(R.string.app_short_name), 16f, palette.text, bold = true))
        val status = Ui.label(context, "", 12f, palette.subtle)
        titles.addView(status)
        header.addView(Ui.weighted(titles))
        val speak = CheckBox(context)
        speak.text = context.getString(R.string.overlay_read_aloud)
        speak.setTextColor(palette.subtle)
        speak.textSize = 12f
        speak.isChecked = prefs.speakReplies
        speak.setOnCheckedChangeListener { _, checked ->
            prefs.speakReplies = checked
            if (!checked) Speaker.stop()
        }
        header.addView(speak)
        header.addView(Ui.button(context, palette, context.getString(R.string.action_close)) { hidePanel() })
        root.addView(header)

        // Conversation.
        val scroll = ScrollView(context)
        val list = LinearLayout(context)
        list.orientation = LinearLayout.VERTICAL
        scroll.addView(list, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        val scrollParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            (metrics.heightPixels * 0.26f).toInt().coerceIn(Ui.dp(context, 120), Ui.dp(context, 280))
        )
        scrollParams.topMargin = Ui.dp(context, 10)
        root.addView(scroll, scrollParams)

        // Actions waiting for the owner's approval.
        val approvals = LinearLayout(context)
        approvals.orientation = LinearLayout.VERTICAL
        root.addView(approvals)

        // Input row.
        val inputRow = Ui.row(context)
        val field = Ui.field(context, palette, context.getString(R.string.overlay_hint), InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES)
        field.imeOptions = EditorInfo.IME_ACTION_SEND
        field.maxLines = 3
        field.setOnEditorActionListener { _, action, _ ->
            if (action == EditorInfo.IME_ACTION_SEND) {
                submitInput()
                true
            } else {
                false
            }
        }
        val fieldParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        fieldParams.rightMargin = Ui.dp(context, 8)
        inputRow.addView(field, fieldParams)
        val mic = Ui.button(context, palette, context.getString(R.string.action_mic)) { toggleListening() }
        val micParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        micParams.rightMargin = Ui.dp(context, 8)
        inputRow.addView(mic, micParams)
        val send = Ui.button(context, palette, context.getString(R.string.action_send), primary = true) { submitInput() }
        inputRow.addView(send)
        val rowParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        rowParams.topMargin = Ui.dp(context, 4)
        root.addView(inputRow, rowParams)

        val width = (metrics.widthPixels - Ui.dp(context, 24)).coerceAtMost(Ui.dp(context, 420))
        val params = WindowManager.LayoutParams(
            width, ViewGroup.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL or WindowManager.LayoutParams.FLAG_WATCH_OUTSIDE_TOUCH,
            PixelFormat.TRANSLUCENT
        )
        // Anchored near the top so the keyboard never covers the input field.
        params.gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
        params.y = Ui.dp(context, 40)
        params.softInputMode = WindowManager.LayoutParams.SOFT_INPUT_STATE_UNCHANGED
        try {
            manager.addView(root, params)
        } catch (_: RuntimeException) {
            return
        }
        panel = root
        messageList = list
        messageScroll = scroll
        approvalsBox = approvals
        input = field
        statusLabel = status
        micButton = mic
        sendButton = send

        if (transcript.isEmpty()) {
            transcript.add(Line(false, context.getString(if (prefs.canSync) R.string.overlay_greeting else R.string.overlay_greeting_unpaired)))
        }
        for (line in transcript) line.view = addBubbleView(line.fromUser, line.text)
        scrollToEnd()
        renderStatus()
        applyBusy()
        refreshApprovals()
    }

    private fun hidePanel() {
        val view = panel ?: return
        stopListening()
        removeWindow(view)
        for (line in transcript) line.view = null
        panel = null
        messageList = null
        messageScroll = null
        approvalsBox = null
        input = null
        statusLabel = null
        micButton = null
        sendButton = null
    }

    private fun renderStatus() {
        val label = statusLabel ?: return
        val state = Hub.linkState
        when {
            !prefs.isPaired -> {
                label.text = context.getString(R.string.status_not_paired)
                label.setTextColor(palette.warn)
            }
            prefs.revoked -> {
                label.text = context.getString(R.string.status_revoked)
                label.setTextColor(palette.bad)
            }
            state.connected -> {
                label.text = context.getString(R.string.status_connected, prefs.coreName)
                label.setTextColor(palette.good)
            }
            else -> {
                label.text = context.getString(R.string.overlay_offline)
                label.setTextColor(palette.warn)
            }
        }
    }

    // ---- conversation views --------------------------------------------

    private fun addBubbleView(fromUser: Boolean, text: String): TextView? {
        val list = messageList ?: return null
        val view = Ui.label(context, text, 14f, palette.text)
        view.background = Ui.rounded(if (fromUser) palette.userBubble else palette.field, Ui.dp(context, 14).toFloat())
        view.setPadding(Ui.dp(context, 12), Ui.dp(context, 8), Ui.dp(context, 12), Ui.dp(context, 8))
        val params = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        params.gravity = if (fromUser) Gravity.END else Gravity.START
        params.topMargin = Ui.dp(context, 6)
        if (fromUser) params.leftMargin = Ui.dp(context, 36) else params.rightMargin = Ui.dp(context, 36)
        list.addView(view, params)
        return view
    }

    private fun scrollToEnd() {
        val scroll = messageScroll ?: return
        scroll.post { scroll.fullScroll(View.FOCUS_DOWN) }
    }

    /** Adds a message to the transcript and, if the panel is open, to the screen. Main thread only. */
    private fun say(fromUser: Boolean, text: String): Line {
        val line = Line(fromUser, text)
        transcript.add(line)
        while (transcript.size > MAX_TRANSCRIPT) transcript.removeAt(0)
        line.view = addBubbleView(fromUser, text)
        scrollToEnd()
        return line
    }

    private fun applyBusy() {
        sendButton?.isEnabled = !busy
        sendButton?.alpha = if (busy) 0.5f else 1f
    }

    private fun setBusy(value: Boolean) {
        Hub.runOnMain {
            busy = value
            applyBusy()
        }
    }

    private fun submitInput() {
        val field = input ?: return
        val text = field.text?.toString()?.trim().orEmpty()
        if (text.isEmpty() || busy) return
        field.setText("")
        ask(text)
    }

    // ---- asking Felix ---------------------------------------------------

    /** A reply that is still arriving: one transcript line whose bubble grows as text streams in. */
    private inner class LiveReply {
        private var line: Line? = null // main thread only
        private val text = StringBuilder() // worker thread only

        val isEmpty: Boolean get() = text.isEmpty()

        fun append(delta: String) {
            if (delta.isEmpty()) return
            text.append(delta)
            show(text.toString())
        }

        fun replace(full: String) {
            text.setLength(0)
            text.append(full)
            show(full)
        }

        /** Shows a progress note in the bubble until the first real text arrives. */
        fun hint(note: String) {
            if (text.isEmpty()) show(note)
        }

        fun current(): String = text.toString()

        private fun show(value: String) {
            Hub.runOnMain {
                val existing = line
                if (existing == null) {
                    line = say(false, value)
                } else {
                    existing.text = value
                    existing.view?.text = value
                    scrollToEnd()
                }
            }
        }
    }

    /** Runs [block] on the worker thread. A failure there must never take the whole app down. */
    private fun onWorker(block: () -> Unit): Boolean = try {
        worker.execute {
            try {
                block()
            } catch (e: RuntimeException) {
                val detail = e.message ?: e.javaClass.simpleName
                Hub.runOnMain { if (!destroyed) say(false, context.getString(R.string.overlay_failed, detail)) }
            }
        }
        true
    } catch (_: RejectedExecutionException) {
        false
    }

    private fun ask(text: String) {
        say(true, text)
        busy = true
        applyBusy()
        val started = onWorker {
            try {
                converse(text)
            } finally {
                setBusy(false)
            }
        }
        if (!started) {
            busy = false
            applyBusy()
        }
    }

    private fun converse(text: String) {
        if (!prefs.canSync) {
            answerOffline(text, context.getString(if (prefs.revoked) R.string.status_revoked else R.string.overlay_unpaired_hint))
            return
        }
        val live = LiveReply()
        var finished: ChatReply? = null
        var streamError: String? = null
        val extraApprovals = ArrayList<Approval>()
        val parser = SseParser { name, data ->
            val json = Json.parseObject(data)
            when (name) {
                "delta" -> live.append(json.strOr("text", ""))
                "tool" -> if (json.str("status") == "start") live.hint(context.getString(R.string.overlay_working))
                "approval" -> Wire.parseApproval(json)?.let { extraApprovals.add(it) }
                "done" -> finished = Wire.parseChatReply(json)
                "error" -> streamError = json.str("message") ?: "Felix Core reported an error."
            }
        }
        try {
            val response = client.stream("/api/chat/stream", Wire.chatBody(text)) { parser.feedLine(it) }
            parser.finish()
            when {
                response.code == 200 -> {
                    val done = finished
                    if (done != null && done.reply.isNotEmpty()) live.replace(done.reply)
                    streamError?.let { live.replace(if (live.isEmpty) it else live.current() + "\n\n" + it) }
                    if (live.isEmpty) live.replace(context.getString(R.string.overlay_no_answer))
                    afterReply(live.current(), (done?.approvals ?: emptyList()) + extraApprovals)
                }
                response.code == 401 -> {
                    prefs.revoked = true
                    live.replace(context.getString(R.string.status_revoked))
                }
                response.code == 404 || response.code == 405 || response.code == 501 -> askWithoutStreaming(text, live)
                else -> live.replace(context.getString(R.string.overlay_core_error, response.code))
            }
        } catch (e: CoreUnreachable) {
            if (live.isEmpty) answerOffline(text, e.message ?: context.getString(R.string.overlay_offline)) else live.append("\n\n" + context.getString(R.string.overlay_connection_lost))
        } catch (e: IOException) {
            // The request reached Core, so it is not sent a second time.
            if (live.isEmpty) live.replace(context.getString(R.string.overlay_connection_lost)) else live.append("\n\n" + context.getString(R.string.overlay_connection_lost))
        }
    }

    @Throws(IOException::class)
    private fun askWithoutStreaming(text: String, live: LiveReply) {
        val response = client.request("POST", "/api/chat", Wire.chatBody(text), 120_000)
        if (!response.ok) {
            live.replace(context.getString(R.string.overlay_core_error, response.code))
            return
        }
        val reply = Wire.parseChatReply(Json.parseObject(response.body))
        live.replace(reply.reply.ifEmpty { context.getString(R.string.overlay_no_answer) })
        afterReply(live.current(), reply.approvals)
    }

    private fun afterReply(reply: String, approvals: List<Approval>) {
        if (prefs.speakReplies) Speaker.speak(service, reply)
        if (approvals.isNotEmpty()) {
            Hub.runOnMain { renderApprovals(approvals.distinctBy { it.id }) }
        } else {
            refreshApprovals()
        }
    }

    private fun answerOffline(text: String, why: String) {
        val parsed = OfflineCommands.parse(text)
        val message = if (parsed != null) {
            executor.runLocal(parsed)
        } else {
            why + "\n\n" + OfflineCommands.HELP
        }
        Hub.runOnMain { say(false, message) }
        if (prefs.speakReplies && parsed != null) Speaker.speak(service, message)
    }

    // ---- approvals ------------------------------------------------------

    private fun refreshApprovals() {
        if (!prefs.canSync || destroyed) return
        onWorker {
            val approvals = try {
                val response = client.request("GET", "/api/approvals?status=pending", null, 10_000)
                if (response.ok) Wire.parseApprovals(response.body) else null
            } catch (_: IOException) {
                null
            }
            if (approvals != null) Hub.runOnMain { renderApprovals(approvals) }
        }
    }

    private fun renderApprovals(approvals: List<Approval>) {
        val box = approvalsBox ?: return
        box.removeAllViews()
        for (approval in approvals.take(4)) {
            val item = LinearLayout(context)
            item.orientation = LinearLayout.VERTICAL
            item.background = Ui.rounded(palette.field, Ui.dp(context, 12).toFloat(), palette.warn, Ui.dp(context, 1))
            item.setPadding(Ui.dp(context, 12), Ui.dp(context, 10), Ui.dp(context, 12), Ui.dp(context, 10))
            item.addView(Ui.label(context, context.getString(R.string.overlay_needs_approval), 12f, palette.warn, bold = true))
            val preview = Ui.label(context, approval.preview.ifEmpty { approval.tool }, 13f, palette.text)
            preview.maxLines = 6
            item.addView(preview)
            val actions = Ui.row(context)
            actions.gravity = Gravity.END
            val deny = Ui.button(context, palette, context.getString(R.string.action_deny)) { decide(approval, approve = false) }
            val denyParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            denyParams.rightMargin = Ui.dp(context, 8)
            actions.addView(deny, denyParams)
            actions.addView(Ui.button(context, palette, context.getString(R.string.action_approve), primary = true) { decide(approval, approve = true) })
            val actionParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            actionParams.topMargin = Ui.dp(context, 6)
            item.addView(actions, actionParams)
            val itemParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            itemParams.topMargin = Ui.dp(context, 8)
            box.addView(item, itemParams)
        }
    }

    private fun decide(approval: Approval, approve: Boolean) {
        approvalsBox?.removeAllViews()
        onWorker {
            val verb = if (approve) "approve" else "deny"
            val message = try {
                val response = client.request("POST", "/api/approvals/${approval.id}/$verb", "{}", 60_000)
                val json = Json.parseObject(response.body)
                when {
                    response.ok -> json.str("message")?.takeIf { it.isNotBlank() }
                        ?: context.getString(if (approve) R.string.overlay_approved else R.string.overlay_denied)
                    else -> json.str("message") ?: json.str("error") ?: context.getString(R.string.overlay_core_error, response.code)
                }
            } catch (e: IOException) {
                e.message ?: context.getString(R.string.overlay_offline)
            }
            Hub.runOnMain { say(false, message) }
            refreshApprovals()
        }
    }

    // ---- voice ----------------------------------------------------------

    private fun toggleListening() {
        if (listening) {
            stopListening()
            return
        }
        if (!Permissions.microphone(service)) {
            say(false, context.getString(R.string.overlay_mic_permission))
            return
        }
        if (!SpeechRecognizer.isRecognitionAvailable(service)) {
            say(false, context.getString(R.string.overlay_mic_unavailable))
            return
        }
        val online = DeviceStatus.hasNetwork(service)
        try {
            recognizer?.destroy()
            // Without a network only the on-device recogniser can work.
            val engine = if (!online && Build.VERSION.SDK_INT >= 31 && SpeechRecognizer.isOnDeviceRecognitionAvailable(service)) {
                SpeechRecognizer.createOnDeviceSpeechRecognizer(service)
            } else {
                SpeechRecognizer.createSpeechRecognizer(service)
            }
            engine.setRecognitionListener(listener)
            val request = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH)
                .putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                .putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                .putExtra(RecognizerIntent.EXTRA_CALLING_PACKAGE, service.packageName)
                .putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, !online)
            engine.startListening(request)
            recognizer = engine
            listening = true
            micButton?.text = context.getString(R.string.action_stop)
        } catch (_: RuntimeException) {
            listening = false
            say(false, context.getString(R.string.overlay_mic_unavailable))
        }
    }

    private fun stopListening() {
        if (!listening) return
        listening = false
        micButton?.text = context.getString(R.string.action_mic)
        try {
            recognizer?.stopListening()
        } catch (_: RuntimeException) {
        }
    }

    private val listener = object : RecognitionListener {
        override fun onReadyForSpeech(params: Bundle?) = Unit
        override fun onBeginningOfSpeech() = Unit
        override fun onRmsChanged(rmsdB: Float) = Unit
        override fun onBufferReceived(buffer: ByteArray?) = Unit
        override fun onEndOfSpeech() = Unit
        override fun onEvent(eventType: Int, params: Bundle?) = Unit

        override fun onPartialResults(partialResults: Bundle?) {
            val heard = partialResults?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()
            if (!heard.isNullOrBlank()) input?.setText(heard)
        }

        override fun onResults(results: Bundle?) {
            listening = false
            micButton?.text = context.getString(R.string.action_mic)
            val heard = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull()?.trim()
            if (!heard.isNullOrEmpty()) {
                input?.setText(heard)
                submitInput()
            }
        }

        override fun onError(error: Int) {
            listening = false
            micButton?.text = context.getString(R.string.action_mic)
            val quiet = error == SpeechRecognizer.ERROR_NO_MATCH || error == SpeechRecognizer.ERROR_SPEECH_TIMEOUT ||
                error == SpeechRecognizer.ERROR_CLIENT
            if (!quiet && panel != null) say(false, context.getString(R.string.overlay_mic_failed))
        }
    }

    companion object {
        private const val MAX_TRANSCRIPT = 60
    }
}
