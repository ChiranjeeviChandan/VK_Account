package ai.felix.companion.ui

import ai.felix.companion.Hub
import ai.felix.companion.Prefs
import ai.felix.companion.R
import ai.felix.companion.core.Json
import ai.felix.companion.core.PairingInfo
import ai.felix.companion.core.PairingLink
import ai.felix.companion.core.Wire
import ai.felix.companion.core.bool
import ai.felix.companion.core.obj
import ai.felix.companion.core.str
import ai.felix.companion.core.strOr
import ai.felix.companion.device.Controls
import ai.felix.companion.device.Permissions
import ai.felix.companion.link.FelixLinkService
import ai.felix.companion.net.CoreClient
import ai.felix.companion.net.CoreUnreachable
import ai.felix.companion.net.Endpoint
import ai.felix.companion.notify.FelixNotificationListener
import ai.felix.companion.notify.ReplyActions
import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.ActivityNotFoundException
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowInsetsController
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import java.io.IOException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException

/**
 * The companion's only screen: link status, pairing, the permission checklist,
 * the overlay switch, the latest brief and the list of apps Felix may read.
 */
class MainActivity : Activity() {
    private lateinit var prefs: Prefs
    private lateinit var palette: Palette
    private val worker: ExecutorService = Executors.newSingleThreadExecutor { Thread(it, "felix-ui") }
    private val linkListener: () -> Unit = { renderStatus() }
    private var alive = false
    private var settingSwitches = false

    private lateinit var statusDot: View
    private lateinit var statusTitle: TextView
    private lateinit var statusDetail: TextView
    private lateinit var pairedActions: LinearLayout
    private lateinit var pairCard: LinearLayout
    private lateinit var pairMessage: TextView
    private lateinit var hostField: EditText
    private lateinit var portField: EditText
    private lateinit var codeField: EditText
    private lateinit var pinField: EditText
    private lateinit var httpCheck: CheckBox
    private lateinit var permissionRows: LinearLayout
    private lateinit var overlaySwitch: Switch
    private lateinit var speakSwitch: Switch
    private lateinit var briefMeta: TextView
    private lateinit var briefBody: TextView
    private lateinit var appRows: LinearLayout

    private class Grant(val title: Int, val why: Int, val granted: () -> Boolean, val request: () -> Unit)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Hub.prefs(this)
        palette = Palette.of(this)
        alive = true
        styleSystemBars()
        buildScreen()
        if (savedInstanceState == null) handleIntent(intent)
        FelixLinkService.startIfNeeded(this)
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        Hub.addListener(linkListener)
        renderAll()
        // A permission granted in system settings may be exactly what the service was waiting for.
        FelixLinkService.refresh(this)
        loadBrief(silent = true)
    }

    override fun onPause() {
        Hub.removeListener(linkListener)
        super.onPause()
    }

    override fun onDestroy() {
        alive = false
        worker.shutdownNow()
        super.onDestroy()
    }

    private fun styleSystemBars() {
        window.decorView.setBackgroundColor(palette.background)
        if (Build.VERSION.SDK_INT >= 30) {
            val light = WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS or WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS
            window.insetsController?.setSystemBarsAppearance(if (palette.night) 0 else light, light)
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = if (palette.night) {
                0
            } else {
                View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR or View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
            }
        }
    }

    // ---- building the screen -------------------------------------------

    private fun title(text: Int): TextView {
        val view = Ui.label(this, getString(text), 17f, palette.text, bold = true)
        view.setPadding(0, 0, 0, Ui.dp(this, 6))
        return view
    }

    private fun body(text: CharSequence): TextView = Ui.label(this, text, 14f, palette.subtle)

    private fun buildScreen() {
        val scroll = ScrollView(this)
        scroll.id = R.id.main_scroll
        scroll.fitsSystemWindows = true
        scroll.clipToPadding = false
        scroll.isFillViewport = true
        scroll.setBackgroundColor(palette.background)
        val column = LinearLayout(this)
        column.orientation = LinearLayout.VERTICAL
        val pad = Ui.dp(this, 16)
        column.setPadding(pad, pad, pad, pad)
        scroll.addView(column, ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))

        // Header.
        val header = Ui.row(this)
        val mark = View(this)
        mark.background = FelixMark(palette.accent, palette.onAccent)
        val markParams = LinearLayout.LayoutParams(Ui.dp(this, 40), Ui.dp(this, 40))
        markParams.rightMargin = Ui.dp(this, 12)
        header.addView(mark, markParams)
        val names = LinearLayout(this)
        names.orientation = LinearLayout.VERTICAL
        names.addView(Ui.label(this, getString(R.string.app_name), 22f, palette.text, bold = true))
        names.addView(Ui.label(this, getString(R.string.app_tagline), 13f, palette.subtle))
        header.addView(Ui.weighted(names))
        val headerParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        headerParams.bottomMargin = Ui.dp(this, 16)
        column.addView(header, headerParams)

        // Status.
        val status = Ui.card(this, palette)
        val statusRow = Ui.row(this)
        statusDot = Ui.dot(this, palette.subtle)
        statusRow.addView(statusDot)
        statusTitle = Ui.label(this, "", 17f, palette.text, bold = true)
        statusRow.addView(Ui.weighted(statusTitle))
        status.addView(statusRow)
        statusDetail = body("")
        statusDetail.setPadding(0, Ui.dp(this, 4), 0, 0)
        status.addView(statusDetail)
        pairedActions = Ui.row(this)
        val check = Ui.button(this, palette, getString(R.string.action_check_connection), primary = true) { checkConnection() }
        val checkParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        checkParams.rightMargin = Ui.dp(this, 8)
        pairedActions.addView(check, checkParams)
        pairedActions.addView(Ui.button(this, palette, getString(R.string.action_unpair)) { confirmUnpair() })
        val actionParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        actionParams.topMargin = Ui.dp(this, 12)
        status.addView(pairedActions, actionParams)
        column.addView(status)

        // Pairing.
        pairCard = Ui.card(this, palette)
        pairCard.addView(title(R.string.pair_title))
        pairCard.addView(body(getString(R.string.pair_instructions)))
        hostField = Ui.field(this, palette, getString(R.string.pair_host_hint), InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        hostField.id = R.id.field_host
        portField = Ui.field(this, palette, getString(R.string.pair_port_hint), InputType.TYPE_CLASS_NUMBER)
        portField.id = R.id.field_port
        portField.setText("8766")
        codeField = Ui.field(this, palette, getString(R.string.pair_code_hint), InputType.TYPE_CLASS_NUMBER)
        codeField.id = R.id.field_code
        pinField = Ui.field(this, palette, getString(R.string.pair_pin_hint), InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS)
        pinField.id = R.id.field_pin
        httpCheck = CheckBox(this)
        httpCheck.id = R.id.check_http
        httpCheck.text = getString(R.string.pair_http)
        httpCheck.setTextColor(palette.subtle)
        pairCard.addView(hostField)
        pairCard.addView(portField)
        pairCard.addView(codeField)
        pairCard.addView(pinField)
        pairCard.addView(httpCheck)
        pairMessage = Ui.label(this, "", 14f, palette.bad)
        pairMessage.visibility = View.GONE
        pairCard.addView(pairMessage)
        pairCard.addView(Ui.spacer(this, 8))
        pairCard.addView(Ui.button(this, palette, getString(R.string.action_pair), primary = true) { pairFromForm() })
        column.addView(pairCard)

        // Permissions.
        val permissions = Ui.card(this, palette)
        permissions.addView(title(R.string.permissions_title))
        permissions.addView(body(getString(R.string.permissions_intro)))
        if (Build.VERSION.SDK_INT >= 33) {
            val note = body(getString(R.string.permissions_restricted))
            note.setPadding(0, Ui.dp(this, 8), 0, Ui.dp(this, 8))
            permissions.addView(note)
            permissions.addView(Ui.button(this, palette, getString(R.string.action_app_info)) { openAppDetails() })
        }
        permissionRows = LinearLayout(this)
        permissionRows.orientation = LinearLayout.VERTICAL
        permissions.addView(permissionRows)
        column.addView(permissions)

        // Overlay.
        val overlay = Ui.card(this, palette)
        overlay.addView(title(R.string.overlay_title))
        overlaySwitch = Switch(this)
        overlaySwitch.text = getString(R.string.overlay_switch)
        overlaySwitch.setTextColor(palette.text)
        overlaySwitch.textSize = 15f
        overlaySwitch.setOnCheckedChangeListener { _, checked -> if (!settingSwitches) setOverlay(checked) }
        overlay.addView(overlaySwitch)
        overlay.addView(body(getString(R.string.overlay_description)))
        speakSwitch = Switch(this)
        speakSwitch.text = getString(R.string.overlay_read_aloud_long)
        speakSwitch.setTextColor(palette.text)
        speakSwitch.textSize = 15f
        speakSwitch.setOnCheckedChangeListener { _, checked -> if (!settingSwitches) prefs.speakReplies = checked }
        val speakParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        speakParams.topMargin = Ui.dp(this, 8)
        overlay.addView(speakSwitch, speakParams)
        column.addView(overlay)

        // Latest brief.
        val brief = Ui.card(this, palette)
        brief.addView(title(R.string.brief_title))
        briefMeta = Ui.label(this, "", 13f, palette.subtle)
        brief.addView(briefMeta)
        briefBody = Ui.label(this, getString(R.string.brief_empty), 14f, palette.text)
        briefBody.setPadding(0, Ui.dp(this, 6), 0, Ui.dp(this, 10))
        brief.addView(briefBody)
        brief.addView(Ui.button(this, palette, getString(R.string.action_refresh)) { loadBrief(silent = false) })
        column.addView(brief)

        // Notification apps.
        val apps = Ui.card(this, palette)
        apps.addView(title(R.string.apps_title))
        apps.addView(body(getString(R.string.apps_intro)))
        appRows = LinearLayout(this)
        appRows.orientation = LinearLayout.VERTICAL
        apps.addView(appRows)
        apps.addView(Ui.spacer(this, 8))
        apps.addView(Ui.button(this, palette, getString(R.string.action_add_apps)) { chooseApps() })
        column.addView(apps)

        // About.
        val about = Ui.card(this, palette)
        about.addView(title(R.string.about_title))
        about.addView(body(getString(R.string.about_text, versionName())))
        column.addView(about)

        setContentView(scroll)
    }

    private fun versionName(): String = try {
        packageManager.getPackageInfo(packageName, 0).versionName ?: "?"
    } catch (_: PackageManager.NameNotFoundException) {
        "?"
    }

    // ---- rendering ------------------------------------------------------

    private fun renderAll() {
        renderStatus()
        renderPermissions()
        renderApps()
        settingSwitches = true
        overlaySwitch.isChecked = prefs.overlayEnabled
        speakSwitch.isChecked = prefs.speakReplies
        settingSwitches = false
    }

    private fun renderStatus() {
        if (!alive) return
        val state = Hub.linkState
        val paired = prefs.isPaired
        val color: Int
        val headline: String
        val detail: String
        when {
            !paired -> {
                color = palette.warn
                headline = getString(R.string.status_not_paired)
                detail = getString(R.string.status_not_paired_detail)
            }
            prefs.revoked -> {
                color = palette.bad
                headline = getString(R.string.status_revoked_short)
                detail = getString(R.string.status_revoked)
            }
            state.connected -> {
                color = palette.good
                headline = getString(R.string.status_connected, prefs.coreName)
                val host = prefs.activeHost ?: prefs.hosts.firstOrNull().orEmpty()
                val mode = when (state.coreOnline) {
                    true -> getString(R.string.status_core_online)
                    false -> getString(R.string.status_core_offline)
                    null -> ""
                }
                detail = getString(R.string.status_connected_detail, host, prefs.port) + (if (mode.isEmpty()) "" else "\n$mode")
            }
            else -> {
                color = palette.warn
                headline = getString(R.string.status_searching, prefs.coreName)
                detail = state.lastError ?: getString(R.string.status_searching_detail)
            }
        }
        statusDot.background = Ui.rounded(color, Ui.dp(this, 10).toFloat())
        statusTitle.text = headline
        statusDetail.text = detail
        pairedActions.visibility = if (paired) View.VISIBLE else View.GONE
        pairCard.visibility = if (!paired || prefs.revoked) View.VISIBLE else View.GONE
    }

    private fun grants(): List<Grant> {
        val list = ArrayList<Grant>()
        list.add(Grant(R.string.perm_notifications, R.string.perm_notifications_why, { Permissions.notificationListener(this) }) {
            val detail = Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
            if (Build.VERSION.SDK_INT >= 30) {
                val direct = Intent(Settings.ACTION_NOTIFICATION_LISTENER_DETAIL_SETTINGS).putExtra(
                    Settings.EXTRA_NOTIFICATION_LISTENER_COMPONENT_NAME,
                    ComponentName(this, FelixNotificationListener::class.java).flattenToString()
                )
                openSettings(direct, detail)
            } else {
                openSettings(detail)
            }
        })
        list.add(Grant(R.string.perm_accessibility, R.string.perm_accessibility_why, { Permissions.accessibility(this) }) {
            openSettings(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        })
        list.add(Grant(R.string.perm_overlay, R.string.perm_overlay_why, { Permissions.overlay(this) }) {
            openSettings(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, packageUri()), Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION))
        })
        list.add(Grant(R.string.perm_battery, R.string.perm_battery_why, { Permissions.batteryUnrestricted(this) }) {
            openSettings(
                Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, packageUri()),
                Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
            )
        })
        list.add(Grant(R.string.perm_post, R.string.perm_post_why, { Permissions.postNotifications(this) }) {
            if (Build.VERSION.SDK_INT >= 33 && !Permissions.has(this, Manifest.permission.POST_NOTIFICATIONS)) {
                ask(Manifest.permission.POST_NOTIFICATIONS)
            } else {
                openSettings(Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, packageName))
            }
        })
        list.add(Grant(R.string.perm_contacts, R.string.perm_contacts_why, { Permissions.contacts(this) }) {
            ask(Manifest.permission.READ_CONTACTS)
        })
        list.add(Grant(R.string.perm_calendar, R.string.perm_calendar_why, { Permissions.calendar(this) }) {
            ask(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR)
        })
        list.add(Grant(R.string.perm_sms, R.string.perm_sms_why, {
            Permissions.hasAll(this, Manifest.permission.SEND_SMS, Manifest.permission.READ_SMS, Manifest.permission.RECEIVE_SMS)
        }) {
            ask(Manifest.permission.SEND_SMS, Manifest.permission.READ_SMS, Manifest.permission.RECEIVE_SMS)
        })
        list.add(Grant(R.string.perm_phone, R.string.perm_phone_why, {
            Permissions.hasAll(this, Manifest.permission.CALL_PHONE, Manifest.permission.READ_PHONE_STATE, Manifest.permission.READ_CALL_LOG)
        }) {
            ask(Manifest.permission.CALL_PHONE, Manifest.permission.READ_PHONE_STATE, Manifest.permission.READ_CALL_LOG)
        })
        list.add(Grant(R.string.perm_location, R.string.perm_location_why, {
            Permissions.location(this) && Permissions.backgroundLocation(this)
        }) {
            if (!Permissions.location(this)) {
                ask(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)
            } else if (Build.VERSION.SDK_INT >= 29) {
                // Android only grants "all the time" in a second, separate step.
                ask(Manifest.permission.ACCESS_BACKGROUND_LOCATION)
            }
        })
        list.add(Grant(R.string.perm_microphone, R.string.perm_microphone_why, { Permissions.microphone(this) }) {
            ask(Manifest.permission.RECORD_AUDIO)
        })
        list.add(Grant(R.string.perm_dnd, R.string.perm_dnd_why, { Permissions.doNotDisturbAccess(this) }) {
            openSettings(Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS))
        })
        list.add(Grant(R.string.perm_settings, R.string.perm_settings_why, { Permissions.writeSettings(this) }) {
            openSettings(Intent(Settings.ACTION_MANAGE_WRITE_SETTINGS, packageUri()), Intent(Settings.ACTION_MANAGE_WRITE_SETTINGS))
        })
        return list
    }

    private fun renderPermissions() {
        permissionRows.removeAllViews()
        for (grant in grants()) {
            val granted = try {
                grant.granted()
            } catch (_: RuntimeException) {
                false
            }
            permissionRows.addView(Ui.divider(this, palette))
            val head = Ui.row(this)
            head.addView(Ui.weighted(Ui.label(this, getString(grant.title), 15f, palette.text, bold = true)))
            head.addView(
                Ui.label(
                    this, getString(if (granted) R.string.perm_granted else R.string.perm_needed), 13f,
                    if (granted) palette.good else palette.warn, bold = true
                )
            )
            permissionRows.addView(head)
            permissionRows.addView(body(getString(grant.why)))
            if (!granted) {
                val button = Ui.button(this, palette, getString(R.string.action_grant)) { grant.request() }
                val params = LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                params.topMargin = Ui.dp(this, 8)
                permissionRows.addView(button, params)
            }
        }
    }

    private fun renderApps() {
        appRows.removeAllViews()
        val selected = prefs.notificationApps.sorted()
        if (selected.isEmpty()) {
            appRows.addView(body(getString(R.string.apps_none)))
            return
        }
        for (pkg in selected) {
            appRows.addView(Ui.divider(this, palette))
            val row = Ui.row(this)
            val names = LinearLayout(this)
            names.orientation = LinearLayout.VERTICAL
            names.addView(Ui.label(this, appLabel(pkg), 15f, palette.text, bold = true))
            names.addView(Ui.label(this, pkg, 12f, palette.subtle))
            row.addView(Ui.weighted(names))
            row.addView(Ui.button(this, palette, getString(R.string.action_remove)) {
                prefs.notificationApps = prefs.notificationApps - pkg
                renderApps()
            })
            appRows.addView(row)
        }
    }

    private fun appLabel(pkg: String): String = try {
        packageManager.getApplicationLabel(packageManager.getApplicationInfo(pkg, 0)).toString()
    } catch (_: PackageManager.NameNotFoundException) {
        getString(R.string.apps_not_installed)
    }

    // ---- permissions ----------------------------------------------------

    private fun packageUri(): Uri = Uri.fromParts("package", packageName, null)

    private fun openSettings(vararg candidates: Intent) {
        for (intent in candidates) {
            try {
                startActivity(intent)
                return
            } catch (_: ActivityNotFoundException) {
                // Try the next, more general screen.
            } catch (_: SecurityException) {
            }
        }
        openAppDetails()
    }

    private fun openAppDetails() {
        try {
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, packageUri()))
        } catch (_: ActivityNotFoundException) {
            message(getString(R.string.app_name), getString(R.string.settings_unavailable))
        }
    }

    private fun ask(vararg permissions: String) {
        val missing = permissions.filter { !Permissions.has(this, it) }
        if (missing.isEmpty()) {
            renderPermissions()
            return
        }
        requestPermissions(missing.toTypedArray(), REQUEST_PERMISSIONS)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_PERMISSIONS) return
        renderPermissions()
        FelixLinkService.refresh(this)
        val refusedForGood = permissions.indices.any { index ->
            grantResults.getOrNull(index) != PackageManager.PERMISSION_GRANTED && !shouldShowRequestPermissionRationale(permissions[index])
        }
        if (refusedForGood) {
            AlertDialog.Builder(this)
                .setTitle(R.string.permissions_title)
                .setMessage(R.string.permissions_blocked)
                .setPositiveButton(R.string.action_app_info) { _, _ -> openAppDetails() }
                .setNegativeButton(R.string.action_cancel, null)
                .show()
        }
    }

    // ---- overlay --------------------------------------------------------

    private fun setOverlay(enabled: Boolean) {
        prefs.overlayEnabled = enabled
        if (enabled && !Permissions.overlay(this)) {
            openSettings(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, packageUri()), Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION))
        }
        FelixLinkService.refresh(this)
    }

    // ---- intents --------------------------------------------------------

    private fun handleIntent(intent: Intent?) {
        if (intent == null) return
        when (intent.action) {
            Intent.ACTION_VIEW -> {
                val link = intent.dataString
                if (link != null && link.startsWith("felix:", ignoreCase = true)) pairFromLink(link)
            }
            ACTION_SHOW_TEXT -> message(
                intent.getStringExtra(EXTRA_TITLE) ?: getString(R.string.app_name),
                intent.getStringExtra(EXTRA_TEXT).orEmpty()
            )
        }
    }

    private fun message(title: String, text: String) {
        if (!alive || isFinishing) return
        AlertDialog.Builder(this)
            .setTitle(title)
            .setMessage(text)
            .setPositiveButton(R.string.action_ok, null)
            .show()
    }

    // ---- pairing --------------------------------------------------------

    private fun pairFromLink(link: String) {
        val info = try {
            PairingLink.parse(link)
        } catch (e: PairingLink.InvalidLink) {
            message(getString(R.string.pair_title), e.message ?: getString(R.string.pair_failed))
            return
        }
        val security = getString(if (info.scheme == "https") R.string.pair_confirm_secure else R.string.pair_confirm_insecure)
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.pair_confirm_title, info.name))
            .setMessage(getString(R.string.pair_confirm_text, info.hosts.joinToString(", "), info.port) + "\n\n" + security)
            .setPositiveButton(R.string.action_pair) { _, _ -> pair(info) }
            .setNegativeButton(R.string.action_cancel, null)
            .show()
    }

    private fun pairFromForm() {
        val useHttp = httpCheck.isChecked
        val info = try {
            PairingLink.build(
                scheme = if (useHttp) "http" else "https",
                hosts = hostField.text.toString(),
                port = portField.text.toString(),
                code = codeField.text.toString(),
                fingerprint = if (useHttp) null else pinField.text.toString(),
                name = null,
            )
        } catch (e: PairingLink.InvalidLink) {
            showPairMessage(e.message ?: getString(R.string.pair_failed), error = true)
            return
        }
        pair(info)
    }

    private fun showPairMessage(text: String, error: Boolean) {
        pairMessage.text = text
        pairMessage.setTextColor(if (error) palette.bad else palette.subtle)
        pairMessage.visibility = View.VISIBLE
    }

    private fun pair(info: PairingInfo) {
        showPairMessage(getString(R.string.pair_working), error = false)
        val deviceName = try {
            Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME)
        } catch (_: SecurityException) {
            null
        }?.takeIf { it.isNotBlank() } ?: Build.MODEL
        val model = listOf(Build.MANUFACTURER, Build.MODEL).filter { !it.isNullOrBlank() }.joinToString(" ")
        val body = Wire.pairBody(info.code, prefs.deviceId, deviceName, model, "Android ${Build.VERSION.RELEASE}", versionName())
        background {
            val target = Endpoint(info.scheme, info.hosts, info.port, info.fingerprint, null)
            var problem = getString(R.string.pair_unreachable)
            var done = false
            for (host in info.hosts) {
                try {
                    val response = CoreClient.requestOnce(target, host, "POST", "/api/phone/pair", body)
                    val parsed = if (response.ok) Wire.parsePairResponse(response.body) else null
                    when {
                        parsed != null -> {
                            prefs.savePairing(info, parsed.token, host, parsed.coreName.ifBlank { info.name }, parsed.pollWait)
                            done = true
                        }
                        response.code == 403 -> problem = getString(R.string.pair_invalid_code)
                        response.code == 429 -> problem = getString(R.string.pair_too_many)
                        else -> problem = getString(R.string.pair_http_error, response.code)
                    }
                    break // the laptop answered; its verdict stands
                } catch (e: CoreUnreachable) {
                    problem = e.message ?: getString(R.string.pair_unreachable)
                } catch (e: IOException) {
                    // The request may have reached the laptop, and a pairing code works only once.
                    problem = getString(R.string.pair_interrupted)
                    break
                }
            }
            val paired = done
            val failure = problem
            onUi {
                if (paired) {
                    pairMessage.visibility = View.GONE
                    codeField.setText("")
                    FelixLinkService.refresh(this)
                    renderAll()
                    message(getString(R.string.pair_done_title), getString(R.string.pair_done_text, prefs.coreName))
                } else {
                    showPairMessage(failure, error = true)
                }
            }
        }
    }

    private fun confirmUnpair() {
        AlertDialog.Builder(this)
            .setTitle(R.string.unpair_title)
            .setMessage(R.string.unpair_text)
            .setPositiveButton(R.string.action_unpair) { _, _ ->
                prefs.clearPairing()
                ReplyActions.clear()
                Hub.updateLink(false)
                background {
                    Hub.db(this).clearAll()
                    onUi {
                        FelixLinkService.refresh(this)
                        renderAll()
                    }
                }
            }
            .setNegativeButton(R.string.action_cancel, null)
            .show()
    }

    private fun checkConnection() {
        statusDetail.text = getString(R.string.status_checking)
        background {
            val client = CoreClient(prefs)
            val outcome = try {
                val response = client.request("GET", "/api/phone/hello", null, 10_000)
                when {
                    response.ok -> {
                        val json = Json.parseObject(response.body)
                        val online = json.bool("online", true)
                        Hub.setLinkState(Hub.linkState.copy(connected = true, lastContactMs = System.currentTimeMillis(), lastError = null, coreOnline = online))
                        val core = json.obj("core")
                        getString(R.string.status_hello, core.strOr("name", prefs.coreName), core.strOr("version", "?"), json.strOr("mode", "auto"))
                    }
                    response.code == 401 -> {
                        prefs.revoked = true
                        getString(R.string.status_revoked)
                    }
                    else -> getString(R.string.pair_http_error, response.code)
                }
            } catch (e: IOException) {
                Hub.updateLink(false, e.message)
                e.message ?: getString(R.string.pair_unreachable)
            }
            onUi {
                renderStatus()
                message(getString(R.string.action_check_connection), outcome)
                FelixLinkService.refresh(this)
            }
        }
    }

    // ---- latest brief ---------------------------------------------------

    private fun loadBrief(silent: Boolean) {
        if (!prefs.canSync) {
            if (!silent) briefBody.text = getString(R.string.brief_unpaired)
            return
        }
        if (!silent) briefMeta.text = getString(R.string.brief_loading)
        background {
            var meta: String? = null
            var text: String? = null
            var problem: String? = null
            try {
                val response = CoreClient(prefs).request("GET", "/api/brief/latest", null, 15_000)
                if (response.ok) {
                    val json = Json.parseObject(response.body)
                    val body = json.str("text").orEmpty()
                    if (body.isNotBlank()) {
                        text = body
                        val kind = json.strOr("kind", "").replaceFirstChar { it.uppercase() }
                        meta = listOf(kind, json.strOr("day", "")).filter { it.isNotBlank() }.joinToString(" · ")
                    }
                } else if (response.code != 404) {
                    problem = getString(R.string.pair_http_error, response.code)
                }
            } catch (e: IOException) {
                problem = e.message ?: getString(R.string.pair_unreachable)
            }
            val shownMeta = meta
            val shownText = text
            val shownProblem = problem
            onUi {
                when {
                    shownText != null -> {
                        briefMeta.text = shownMeta.orEmpty()
                        briefBody.text = shownText
                    }
                    shownProblem != null -> if (!silent) briefMeta.text = shownProblem
                    else -> {
                        briefMeta.text = ""
                        briefBody.text = getString(R.string.brief_empty)
                    }
                }
            }
        }
    }

    // ---- notification apps ----------------------------------------------

    private fun chooseApps() {
        background {
            val installed = Controls.launchableApps(this).filter { it.second != packageName }
            onUi {
                val current = prefs.notificationApps
                val labels = installed.map { it.first }.toTypedArray<CharSequence>()
                val checked = BooleanArray(installed.size) { installed[it].second in current }
                AlertDialog.Builder(this)
                    .setTitle(R.string.apps_title)
                    .setMultiChoiceItems(labels, checked) { _, which, isChecked -> checked[which] = isChecked }
                    .setPositiveButton(R.string.action_ok) { _, _ ->
                        val visible = installed.map { it.second }.toSet()
                        val chosen = installed.filterIndexed { index, _ -> checked[index] }.map { it.second }
                        // Keep entries for apps that are not installed (or hidden) untouched.
                        prefs.notificationApps = (current - visible) + chosen
                        renderApps()
                    }
                    .setNegativeButton(R.string.action_cancel, null)
                    .show()
            }
        }
    }

    // ---- threading ------------------------------------------------------

    private fun background(block: () -> Unit) {
        try {
            worker.execute {
                try {
                    block()
                } catch (_: RuntimeException) {
                    // A failed background task must not crash the screen.
                }
            }
        } catch (_: RejectedExecutionException) {
        }
    }

    private fun onUi(block: () -> Unit) {
        runOnUiThread {
            if (alive && !isFinishing) block()
        }
    }

    companion object {
        const val ACTION_SHOW_TEXT = "ai.felix.companion.action.SHOW_TEXT"
        const val EXTRA_TITLE = "ai.felix.companion.extra.TITLE"
        const val EXTRA_TEXT = "ai.felix.companion.extra.TEXT"
        private const val REQUEST_PERMISSIONS = 7
    }
}
