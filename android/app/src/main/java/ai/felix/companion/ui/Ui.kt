package ai.felix.companion.ui

import android.content.Context
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ColorFilter
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PixelFormat
import android.graphics.Typeface
import android.graphics.drawable.Drawable
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.text.InputType
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

/** The companion's colours for light and dark mode. Screens are built in code, so nothing depends on theme attributes. */
class Palette(val night: Boolean) {
    val background = if (night) 0xFF101317.toInt() else 0xFFF4F5F7.toInt()
    val card = if (night) 0xFF1A1F26.toInt() else 0xFFFFFFFF.toInt()
    val field = if (night) 0xFF232A33.toInt() else 0xFFF0F2F5.toInt()
    val text = if (night) 0xFFECEFF4.toInt() else 0xFF14181F.toInt()
    val subtle = if (night) 0xFF9AA5B4.toInt() else 0xFF5B6573.toInt()
    val accent = if (night) 0xFF7AA2FF.toInt() else 0xFF2456E6.toInt()
    val onAccent = if (night) 0xFF0B1220.toInt() else 0xFFFFFFFF.toInt()
    val good = if (night) 0xFF5FD39A.toInt() else 0xFF1B7F4B.toInt()
    val warn = if (night) 0xFFF2B45A.toInt() else 0xFFA15C00.toInt()
    val bad = if (night) 0xFFFF8A80.toInt() else 0xFFB3261E.toInt()
    val divider = if (night) 0xFF2A313B.toInt() else 0xFFE3E6EB.toInt()
    val userBubble = if (night) 0xFF24406F.toInt() else 0xFFDCE6FF.toInt()

    companion object {
        fun of(context: Context): Palette {
            val mode = context.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK
            return Palette(mode == Configuration.UI_MODE_NIGHT_YES)
        }
    }
}

/** Small view-building helpers shared by the main screen and the overlay. */
object Ui {
    fun dp(context: Context, value: Float): Int =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, context.resources.displayMetrics).toInt()

    fun dp(context: Context, value: Int): Int = dp(context, value.toFloat())

    fun rounded(color: Int, radiusPx: Float, strokeColor: Int? = null, strokePx: Int = 0): GradientDrawable {
        val shape = GradientDrawable()
        shape.shape = GradientDrawable.RECTANGLE
        shape.cornerRadius = radiusPx
        shape.setColor(color)
        if (strokeColor != null && strokePx > 0) shape.setStroke(strokePx, strokeColor)
        return shape
    }

    private fun pressable(context: Context, color: Int, radiusDp: Int, strokeColor: Int? = null): Drawable {
        val radius = dp(context, radiusDp).toFloat()
        val content = rounded(color, radius, strokeColor, if (strokeColor != null) dp(context, 1) else 0)
        val mask = rounded(Color.WHITE, radius)
        return RippleDrawable(ColorStateList.valueOf(0x33808080), content, mask)
    }

    fun label(context: Context, text: CharSequence, sizeSp: Float, color: Int, bold: Boolean = false): TextView {
        val view = TextView(context)
        view.text = text
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, sizeSp)
        view.setTextColor(color)
        if (bold) view.typeface = Typeface.DEFAULT_BOLD
        view.setLineSpacing(0f, 1.15f)
        return view
    }

    /** A button in the companion's style. [primary] buttons are filled; the others are outlined. */
    fun button(context: Context, palette: Palette, text: CharSequence, primary: Boolean = false, onClick: () -> Unit): Button {
        val view = Button(context)
        view.text = text
        view.isAllCaps = false
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 14f)
        view.typeface = Typeface.DEFAULT_BOLD
        view.stateListAnimator = null
        view.minHeight = dp(context, 44)
        view.minimumHeight = dp(context, 44)
        view.minWidth = dp(context, 64)
        view.minimumWidth = dp(context, 64)
        view.setPadding(dp(context, 16), dp(context, 8), dp(context, 16), dp(context, 8))
        if (primary) {
            view.setTextColor(palette.onAccent)
            view.background = pressable(context, palette.accent, 12)
        } else {
            view.setTextColor(palette.accent)
            view.background = pressable(context, palette.card, 12, palette.divider)
        }
        view.setOnClickListener { onClick() }
        return view
    }

    fun card(context: Context, palette: Palette): LinearLayout {
        val box = LinearLayout(context)
        box.orientation = LinearLayout.VERTICAL
        box.background = rounded(palette.card, dp(context, 16).toFloat(), palette.divider, dp(context, 1))
        val pad = dp(context, 16)
        box.setPadding(pad, pad, pad, pad)
        val params = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        params.bottomMargin = dp(context, 12)
        box.layoutParams = params
        return box
    }

    fun field(context: Context, palette: Palette, hint: CharSequence, inputType: Int = InputType.TYPE_CLASS_TEXT): EditText {
        val view = EditText(context)
        view.hint = hint
        view.inputType = inputType
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, 15f)
        view.setTextColor(palette.text)
        view.setHintTextColor(palette.subtle)
        view.background = rounded(palette.field, dp(context, 10).toFloat())
        view.setPadding(dp(context, 12), dp(context, 10), dp(context, 12), dp(context, 10))
        view.minHeight = dp(context, 44)
        val params = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        params.topMargin = dp(context, 8)
        view.layoutParams = params
        return view
    }

    fun spacer(context: Context, heightDp: Int): View {
        val view = View(context)
        view.layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(context, heightDp))
        return view
    }

    fun divider(context: Context, palette: Palette): View {
        val view = View(context)
        val params = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(context, 1))
        params.topMargin = dp(context, 10)
        params.bottomMargin = dp(context, 10)
        view.layoutParams = params
        view.setBackgroundColor(palette.divider)
        return view
    }

    /** A horizontal row whose children are vertically centred. */
    fun row(context: Context): LinearLayout {
        val view = LinearLayout(context)
        view.orientation = LinearLayout.HORIZONTAL
        view.gravity = Gravity.CENTER_VERTICAL
        return view
    }

    fun weighted(view: View, weight: Float = 1f): View {
        view.layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, weight)
        return view
    }

    fun dot(context: Context, color: Int): View {
        val view = View(context)
        val size = dp(context, 10)
        val params = LinearLayout.LayoutParams(size, size)
        params.rightMargin = dp(context, 8)
        view.layoutParams = params
        view.background = rounded(color, size.toFloat())
        return view
    }
}

/** The Felix mark: a filled disc with a geometric "F". Drawn in code so it needs no image assets. */
class FelixMark(private val discColor: Int, private val glyphColor: Int) : Drawable() {
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val glyph = Path()

    override fun draw(canvas: Canvas) {
        val area = bounds
        val size = minOf(area.width(), area.height()).toFloat()
        if (size <= 0f) return
        val cx = area.exactCenterX()
        val cy = area.exactCenterY()
        paint.style = Paint.Style.FILL
        paint.color = discColor
        canvas.drawCircle(cx, cy, size / 2f, paint)

        // The glyph is laid out on a 24 x 24 grid centred in the disc.
        val unit = size / 24f * 0.62f
        val left = cx - 5.5f * unit
        val top = cy - 7f * unit
        glyph.reset()
        glyph.moveTo(left, top)
        glyph.rLineTo(11f * unit, 0f)
        glyph.rLineTo(0f, 3f * unit)
        glyph.rLineTo(-8f * unit, 0f)
        glyph.rLineTo(0f, 3f * unit)
        glyph.rLineTo(6f * unit, 0f)
        glyph.rLineTo(0f, 3f * unit)
        glyph.rLineTo(-6f * unit, 0f)
        glyph.rLineTo(0f, 5f * unit)
        glyph.rLineTo(-3f * unit, 0f)
        glyph.close()
        paint.color = glyphColor
        canvas.drawPath(glyph, paint)
    }

    override fun setAlpha(alpha: Int) {
        paint.alpha = alpha
    }

    override fun setColorFilter(colorFilter: ColorFilter?) {
        paint.colorFilter = colorFilter
    }

    @Deprecated("Deprecated in the platform")
    override fun getOpacity(): Int = PixelFormat.TRANSLUCENT
}
