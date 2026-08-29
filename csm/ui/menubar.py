"""Menu-bar plan-usage monitor: an NSStatusItem plus a dark popover card.

The status item shows one glanceable percentage (the 5-hour window by default, see
`_headline`). Clicking it opens an NSPopover whose content is a single custom-drawn
card: one block per limit, each with a name, its reset time, a rounded meter and a
"% used" caption.

Why this can be "live": Claude Code itself refreshes ~/.claude.json's
`cachedUsageUtilization` while you work, so re-reading it on a short timer reflects
fresh numbers during an active session. When you haven't used the CLI recently the
snapshot is old — the card says so honestly ("cached 9h ago", a flat grey meter)
rather than presenting stale data as current.

Two rules this file has to keep:
  * Nothing here may raise. `refresh()` is called from an NSTimer block on the main
    thread (coordinator.py), where an exception would escape into AppKit.
  * The card is a FIXED dark surface, so it must never use semantic colours
    (labelColor, secondaryLabelColor, separatorColor …). Those invert in Light Mode
    and would turn the card white-on-white. Every colour below is an explicit sRGB
    literal, built lazily because this module is imported before NSApplication exists.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import AppKit
import objc

from csm import plan as plan_mod

SEVERITY_NSCOLOR = {
    "critical": lambda: AppKit.NSColor.systemRedColor(),
    "warning": lambda: AppKit.NSColor.systemOrangeColor(),
    "normal": lambda: AppKit.NSColor.systemGreenColor(),
}

_SEVERITY_RANK = {"critical": 3, "warning": 2, "normal": 1, None: 0}

# Card order: the 5-hour window leads, then the overall weekly, then any per-model
# weekly caps -- matching the headline the menu-bar number tracks.
_KIND_ORDER = {"session": 0, "weekly_all": 1, "weekly_scoped": 2}


# --------------------------------------------------------------------------- tokens
def _srgb(r, g, b, a=1.0):
    return AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(
        r / 255.0, g / 255.0, b / 255.0, a)


# Glass palette: a near-black panel with a faint top sheen, hairline light edge and
# pill meters — the "clean glass HUD" look of the reference design.
CARD_BG_TOP = lambda: _srgb(26, 26, 29)        # noqa: E731 - lazy, see module docstring
CARD_BG_BOTTOM = lambda: _srgb(11, 11, 13)     # noqa: E731
TRACK = lambda: _srgb(56, 56, 61)              # noqa: E731
TEXT_PRIMARY = lambda: _srgb(255, 255, 255)    # noqa: E731
TEXT_LABEL = lambda: _srgb(245, 245, 247)      # noqa: E731
TEXT_SECONDARY = lambda: _srgb(152, 152, 158)  # noqa: E731
TEXT_TERTIARY = lambda: _srgb(110, 110, 116)   # noqa: E731
HAIRLINE = lambda: _srgb(255, 255, 255, 0.10)  # noqa: E731

# Severity -> (gradient start, gradient end). Keyed by the exact strings plan.py emits.
FILL = {
    "normal": (lambda: _srgb(48, 209, 88), lambda: _srgb(50, 215, 75)),
    # warning must not END on the colour critical BEGINS on, or a full warning bar
    # and a fresh critical one are the same hue.
    "warning": (lambda: _srgb(255, 212, 38), lambda: _srgb(255, 159, 10)),
    "critical": (lambda: _srgb(255, 107, 53), lambda: _srgb(255, 59, 48)),
}
# A stale snapshot gets a flat grey meter: colour would imply the number is current.
# #8E8E93 rather than #5A5A5E -- the latter is only 1.65:1 against the track, so a
# 96% stale bar was indistinguishable from an empty one. This is 3.48:1.
FILL_STALE = lambda: _srgb(142, 142, 147)      # noqa: E731

CARD_W = 340.0
PAD = 16.0
BAR_H = 8.0
CARD_RADIUS = 18.0
HEADER_H = 20.0
HEADER_GAP = 14.0
BLOCK_H = 54.0          # label 17 + gap 7 + bar 8 + gap 6 + caption 16
BLOCK_GAP = 14.0
STALE_H = 24.0
FOOTER_GAP = 12.0
FOOTER_H = 40.0
EMPTY_H = 18.0


def _font(size, weight):
    return AppKit.NSFont.systemFontOfSize_weight_(size, weight)


def _font_digits(size, weight):
    """Monospaced digits, and not for looks: the card refreshes every 30s while it
    is open, so proportional figures make "51 min" -> "50 min" and a ticking
    percentage visibly jitter as the glyph widths change."""
    return AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(size, weight)


def _attr(s, f, color):
    return AppKit.NSAttributedString.alloc().initWithString_attributes_(
        s, {AppKit.NSFontAttributeName: f, AppKit.NSForegroundColorAttributeName: color})


def _limit_name(lim: dict) -> str:
    """Long form used inside the card."""
    kind = lim.get("kind")
    if kind == "session":
        return "Current session"
    if kind == "weekly_all":
        return "All models"
    if kind == "weekly_scoped":
        return lim.get("scopeModel") or "Model limit"
    return kind or "Usage"


def _limit_name_short(lim: dict) -> str:
    """Compact form used in the status-item tooltip."""
    kind = lim.get("kind")
    base = ("5-hour session" if kind == "session"
            else "weekly" if kind in ("weekly_all", "weekly_scoped")
            else kind or "usage")
    scope = lim.get("scopeModel")
    return f"{base} ({scope})" if scope else base


def _ordered(limits):
    return sorted(limits, key=lambda l: (_KIND_ORDER.get(l.get("kind"), 3),
                                         -int(l.get("percent") or 0)))


def _reset_text(iso, now=None) -> str:
    """A near window counts down ("Resets in 51 min"); a far one names the day."""
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    if when.tzinfo is None:
        when = when.astimezone()
    now = now or datetime.now(when.tzinfo or timezone.utc)
    secs = (when - now).total_seconds()
    if secs <= 0:
        return ""
    mins = int(secs // 60)
    if mins < 1:
        return "Resets in under a min"
    if mins < 60:
        return f"Resets in {mins} min"
    if secs < 86400:
        h, m = int(secs // 3600), int((secs % 3600) // 60)
        return f"Resets in {h}h" if m == 0 else f"Resets in {h}h {m}m"
    return f"Resets {when.strftime('%a %-I:%M %p')}"


def _freshness(status: dict):
    """-> (is_live, is_stale, badge).

    `source` alone is not freshness: usage_live.latest() keeps returning the last
    successful fetch forever, and plan.py only marks a *cache* snapshot stale, so a
    dead network or an expired token had the card asserting "live" over hours-old
    numbers. Age decides.
    """
    age = status.get("ageHours")
    live = status.get("source") == "live" and (age is None or age < 0.5)
    stale = bool(status.get("stale")) or (age is not None and age > 6)
    if live:
        badge = "live"
    elif age is None:
        badge = "cached"
    elif age < 1.5:
        badge = "just now"
    elif age < 48:
        badge = f"{round(age)}h ago"
    else:
        badge = f"{round(age / 24)}d ago"
    return live, stale, badge


def card_height(status: dict) -> float:
    if not status.get("available"):
        return PAD + HEADER_H + HEADER_GAP + EMPTY_H + FOOTER_GAP + FOOTER_H
    n = len(status.get("limits") or [])
    body = (n * BLOCK_H + max(0, n - 1) * BLOCK_GAP) if n else EMPTY_H
    # Must use the SAME staleness rule _draw does, or the banner overflows the card.
    _live, is_stale, _badge = _freshness(status)
    stale = STALE_H if is_stale else 0.0
    return PAD + HEADER_H + HEADER_GAP + body + stale + FOOTER_GAP + FOOTER_H


# --------------------------------------------------------------------------- the card
class PlanCardView(AppKit.NSView):
    """Draws the whole card. Only the two footer controls are real subviews, so they
    stay keyboard- and VoiceOver-reachable."""

    def initWithStatus_(self, status):
        self = objc.super(PlanCardView, self).init()
        if self is None:
            return None
        self._status = status or {}
        self.setFrameSize_(AppKit.NSMakeSize(CARD_W, card_height(self._status)))
        return self

    def isFlipped(self):
        return True

    # setStatus_ is a selector so it can be called from anywhere without ceremony.
    def setStatus_(self, status):
        self._status = status or {}
        self.setFrameSize_(AppKit.NSMakeSize(CARD_W, card_height(self._status)))
        self.setNeedsDisplay_(True)

    @objc.python_method
    def status(self):
        return self._status

    @objc.python_method
    def _draw_meter(self, x, y, w, pct, severity, reset, stale):
        track = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSMakeRect(x, y, w, BAR_H), BAR_H / 2, BAR_H / 2)
        TRACK().setFill()
        track.fill()
        if reset or pct <= 0:
            return
        fw = max(BAR_H, w * min(100, max(0, pct)) / 100.0)
        rect = AppKit.NSMakeRect(x, y, fw, BAR_H)
        clip = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            rect, BAR_H / 2, BAR_H / 2)
        ctx = AppKit.NSGraphicsContext.currentContext()
        ctx.saveGraphicsState()
        clip.addClip()
        if stale:
            FILL_STALE().setFill()
            AppKit.NSBezierPath.fillRect_(rect)
        else:
            a, b = FILL.get(severity, FILL["normal"])
            # Ramp across the whole TRACK, not the fill, so a given percentage is
            # always the same hue -- otherwise a short bar compresses the entire
            # ramp and length silently drives colour.
            AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
                a(), b()).drawInRect_angle_(AppKit.NSMakeRect(x, y, w, BAR_H), 0.0)
        ctx.restoreGraphicsState()

    def drawRect_(self, dirty):
        try:
            self._draw()
        except Exception as exc:      # never let a draw bug take down the app
            print(f"plan card draw failed: {type(exc).__name__}: {exc}")

    @objc.python_method
    def _draw(self):
        st = self._status
        bounds = self.bounds()
        # Glass panel: near-black fill with a faint top sheen, then a hairline light
        # edge just inside the clip — the two details that make the card read as a
        # floating HUD instead of a flat grey box.
        shape = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, CARD_RADIUS, CARD_RADIUS)
        shape.addClip()
        # This view is FLIPPED, which inverts NSGradient's angle convention: 270°
        # puts the start colour at the BOTTOM (measured: top #0D0D0F, bottom #18181B
        # — the sheen upside down). 90° is what lights the top edge here.
        AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
            CARD_BG_TOP(), CARD_BG_BOTTOM()).drawInRect_angle_(bounds, 90.0)
        edge = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSInsetRect(bounds, 0.5, 0.5),
            CARD_RADIUS - 0.5, CARD_RADIUS - 0.5)
        HAIRLINE().setStroke()
        edge.setLineWidth_(1.0)
        edge.stroke()

        f_title = _font(15, AppKit.NSFontWeightSemibold)
        f_label = _font(13, AppKit.NSFontWeightMedium)
        f_meta = _font(12, AppKit.NSFontWeightRegular)
        f_num = _font_digits(12, AppKit.NSFontWeightRegular)

        inner = CARD_W - PAD * 2
        y = PAD

        # ---------------------------------------------------------------- header
        glyph = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "chart.bar.fill", "plan usage")
        if glyph is not None:
            cfg = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                13, AppKit.NSFontWeightSemibold)
            glyph = glyph.imageWithSymbolConfiguration_(cfg)
            glyph.setTemplate_(True)
            ctx = AppKit.NSGraphicsContext.currentContext()
            ctx.saveGraphicsState()
            TEXT_PRIMARY().set()
            glyph.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                AppKit.NSMakeRect(PAD, y + 1, 21, 15), AppKit.NSZeroRect,
                AppKit.NSCompositingOperationSourceOver, 1.0, True, None)
            ctx.restoreGraphicsState()
        _attr("Plan Usage", f_title, TEXT_PRIMARY()).drawAtPoint_(
            AppKit.NSMakePoint(PAD + 28, y))

        if st.get("available"):
            is_live, _is_stale, note = _freshness(st)
            dot = FILL["normal"][0]() if is_live else TEXT_SECONDARY()
            s = _attr(note, f_meta, TEXT_SECONDARY())
            sw = s.size().width
            s.drawAtPoint_(AppKit.NSMakePoint(CARD_W - PAD - sw, y + 2))
            dot.setFill()
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                AppKit.NSMakeRect(CARD_W - PAD - sw - 12, y + 7, 6, 6)).fill()
        y += HEADER_H + HEADER_GAP

        # ---------------------------------------------------------------- blocks
        limits = _ordered(st.get("limits") or []) if st.get("available") else []
        if not limits:
            _attr("No plan data yet — run Claude Code", f_meta,
                  TEXT_SECONDARY()).drawAtPoint_(AppKit.NSMakePoint(PAD, y))
            y += EMPTY_H
        else:
            _live, stale, _badge = _freshness(st)
            for i, lim in enumerate(limits):
                _attr(_limit_name(lim), f_label, TEXT_LABEL()).drawAtPoint_(
                    AppKit.NSMakePoint(PAD, y))
                # A reset window has no future reset time, and the caption below
                # already says so — don't print "reset" twice.
                rt = "" if lim.get("reset") else _reset_text(lim.get("resetsAt"))
                if rt:
                    rs = _attr(rt, f_num, TEXT_SECONDARY())
                    rs.drawAtPoint_(AppKit.NSMakePoint(CARD_W - PAD - rs.size().width, y + 1))
                y += 17 + 7
                self._draw_meter(PAD, y, inner, int(lim.get("percent") or 0),
                                 lim.get("severity") or "normal",
                                 bool(lim.get("reset")), stale)
                y += BAR_H + 6
                cap = ("Window reset" if lim.get("reset")
                       else f"{int(lim.get('percent') or 0)}% Used")
                _attr(cap, f_num, TEXT_SECONDARY()).drawAtPoint_(
                    AppKit.NSMakePoint(PAD, y))
                y += 16
                if i != len(limits) - 1:
                    y += BLOCK_GAP
            if stale:
                y += 6
                _attr("Snapshot is stale — open Claude Code to refresh",
                      f_meta, TEXT_TERTIARY()).drawAtPoint_(AppKit.NSMakePoint(PAD, y))
                y += 18

        # ---------------------------------------------------------------- footer rule
        y += FOOTER_GAP
        HAIRLINE().setFill()
        AppKit.NSBezierPath.fillRect_(AppKit.NSMakeRect(PAD, y, inner, 0.5))


# --------------------------------------------------------------------------- ring
def _ring_image(pct, color, diameter=17.0):
    """A rounded-cap progress ring — the reference widget's gauge — drawn into a
    2x bitmap so it stays crisp on retina menu bars; the percentage beside it is
    rendered by the caller. Track uses labelColour so it adapts to light and dark
    menu bars, the arc the severity colour. Returns None on any failure so the
    caller falls back to the SF Symbol gauge."""
    try:
        scale = 2           # Apple's standard @2x — higher scales confuse AppKit scaling
        px = int(diameter * scale)
        rep = AppKit.NSBitmapImageRep.alloc().\
            initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                None, px, px, 8, 4, True, False,
                AppKit.NSCalibratedRGBColorSpace, 0, 0)
        # No rep.setSize_ here: tagging the rep with a smaller point size makes the
        # bitmap context scale user space, and every coordinate drawn below lands 2x
        # off. The untagged rep composites fine — NSImage initWithSize_ already says
        # the image is `diameter` points.
        ctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        if ctx is None:
            return None
        prev = AppKit.NSGraphicsContext.currentContext()
        AppKit.NSGraphicsContext.setCurrentContext_(ctx)
        try:
            centre = AppKit.NSMakePoint(px / 2.0, px / 2.0)
            # Padding for the 2.5pt round caps keeps the stroke inside the bitmap.
            r = px / 2.0 - 2.75 * scale
            lw = 2.5 * scale
            track = AppKit.NSBezierPath.bezierPath()
            # clockwise=False: a 0->360 arc with clockwise=True sweeps the same
            # point both ways and collapses to an empty path — the track would
            # silently never render, leaving pct=0 as a blank status item.
            track.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                centre, r, 0.0, 360.0, False)
            track.setLineWidth_(lw)
            AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.25).setStroke()
            track.stroke()
            sweep = 360.0 * min(100, max(0, pct)) / 100.0
            if sweep > 0:
                arc = AppKit.NSBezierPath.bezierPath()
                # 90° is 12 o'clock in AppKit's angle convention; clockwise=True
                # walks the hand the way a meter fills (12 -> 3 -> 6 -> 9).
                arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                    centre, r, 90.0, 90.0 - sweep, True)
                arc.setLineWidth_(lw)
                arc.setLineCapStyle_(AppKit.NSRoundLineCapStyle)
                color.setStroke()
                arc.stroke()
        finally:
            AppKit.NSGraphicsContext.setCurrentContext_(prev)
        img = AppKit.NSImage.alloc().initWithSize_(AppKit.NSMakeSize(diameter, diameter))
        img.addRepresentation_(rep)
        return img
    except Exception:
        return None


# ----------------------------------------------------------------- status item art
STATUS_CORNER = 13.0     # bottom corner radius of the black tab (soft, not a pill)
STATUS_FLARE = 6.0       # concave fillet where the tab meets the screen edge
STATUS_PAD_X = 8.0
STATUS_RING_D = 15.0
STATUS_GAP = 5.0
STATUS_BG = lambda: _srgb(0, 0, 0, 1.0)         # noqa: E731 - plain black


def _tab_path(w, h, corner=None, flare=None):
    """The tab outline, in an UNFLIPPED box: y=0 is the bottom, y=h the screen edge.

    Convex rounded corners at the bottom, and where it meets the screen edge the sides
    flare OUT to the full width through a concave fillet — the centre of curvature
    sits outside the shape, so the black looks like liquid pulled to the border rather
    than a rectangle stopping at it.
    """
    corner = STATUS_CORNER if corner is None else corner
    flare = STATUS_FLARE if flare is None else flare
    flare = max(0.0, min(flare, w / 2.0))
    corner = max(0.0, min(corner, (w - 2 * flare) / 2.0, h - flare))
    left, right = flare, w - flare              # the body sits inside the flares

    p = AppKit.NSBezierPath.bezierPath()
    p.moveToPoint_(AppKit.NSMakePoint(0.0, h))                 # full width at the edge
    p.lineToPoint_(AppKit.NSMakePoint(w, h))
    # concave fillet, top right: centre OUTSIDE the body
    p.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        AppKit.NSMakePoint(w, h - flare), flare, 90.0, 180.0, False)
    p.lineToPoint_(AppKit.NSMakePoint(right, corner))
    p.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        AppKit.NSMakePoint(right - corner, corner), corner, 0.0, -90.0, True)
    p.lineToPoint_(AppKit.NSMakePoint(left + corner, 0.0))
    p.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        AppKit.NSMakePoint(left + corner, corner), corner, 270.0, 180.0, True)
    p.lineToPoint_(AppKit.NSMakePoint(left, h - flare))
    # concave fillet, top left
    p.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
        AppKit.NSMakePoint(0.0, h - flare), flare, 0.0, 90.0, False)
    p.closePath()
    return p


def _status_image(pct, color, height, show_pct=True, tab=False, draw_bg=None):
    """The whole status item drawn as one image.

    `tab=True` (the popover is open) paints a plain black tab behind the ring and
    percentage: SQUARE top so it runs the full height of the item and reads as part
    of the bezel, rounded bottom so it keeps the pill shape. The caller pins the
    status item's length to this image's width, otherwise macOS centres the image in
    a wider item and its own grey highlight shows down both sides of the black.

    `tab=False` (closed) draws the ring and percentage on transparency, so the item
    looks like any other menu-bar item until you open it.

    Returns None on failure; the caller falls back to the plain SF Symbol.
    """
    # `tab` = the item is wearing the black tab, so style for a black background.
    # `draw_bg` = this image must paint that black itself (no backdrop available).
    if draw_bg is None:
        draw_bg = tab
    try:
        font = AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
            12, AppKit.NSFontWeightMedium)
        text_color = TEXT_PRIMARY() if tab else color
        label = _attr(f"{int(pct)}%", font, text_color) if show_pct else None
        tw = label.size().width if label is not None else 0.0
        width = STATUS_PAD_X * 2 + STATUS_RING_D + (STATUS_GAP + tw if label else 0.0)

        scale = 2
        px_w, px_h = int(width * scale), int(height * scale)
        rep = AppKit.NSBitmapImageRep.alloc().\
            initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                None, px_w, px_h, 8, 4, True, False,
                AppKit.NSCalibratedRGBColorSpace, 0, 0)
        ctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        if ctx is None:
            return None
        prev = AppKit.NSGraphicsContext.currentContext()
        AppKit.NSGraphicsContext.setCurrentContext_(ctx)
        try:
            xf = AppKit.NSAffineTransform.transform()
            xf.scaleBy_(scale)
            xf.concat()

            if draw_bg:
                STATUS_BG().setFill()
                _tab_path(width, height).fill()

            cx = STATUS_PAD_X + STATUS_RING_D / 2.0
            cy = height / 2.0
            r = STATUS_RING_D / 2.0 - 1.6
            lw = 2.3
            # No track on the tab: any unfilled ring reads as a grey circle sitting
            # behind the arc, which is exactly the artefact we are removing. Off the
            # tab a faint track is useful to show the arc's proportion.
            if not tab:
                track = AppKit.NSBezierPath.bezierPath()
                track.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                    AppKit.NSMakePoint(cx, cy), r, 0.0, 360.0, False)
                track.setLineWidth_(lw)
                AppKit.NSColor.labelColor().colorWithAlphaComponent_(0.25).setStroke()
                track.stroke()
            sweep = 360.0 * min(100, max(0, pct)) / 100.0
            if sweep > 0:
                arc = AppKit.NSBezierPath.bezierPath()
                arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
                    AppKit.NSMakePoint(cx, cy), r, 90.0, 90.0 - sweep, True)
                arc.setLineWidth_(lw)
                arc.setLineCapStyle_(AppKit.NSRoundLineCapStyle)
                color.setStroke()
                arc.stroke()

            if label is not None:
                ty = (height - label.size().height) / 2.0
                label.drawAtPoint_(AppKit.NSMakePoint(
                    STATUS_PAD_X + STATUS_RING_D + STATUS_GAP, ty))
        finally:
            AppKit.NSGraphicsContext.setCurrentContext_(prev)

        img = AppKit.NSImage.alloc().initWithSize_(AppKit.NSMakeSize(width, height))
        img.addRepresentation_(rep)
        img.setTemplate_(False)
        return img
    except Exception:
        return None


class _StatusBackdrop(AppKit.NSView):
    """The black tab, drawn behind the status item across the FULL menu-bar height.

    The item's own button is only 22pt tall and macOS reserves ~4pt above and below it
    (and reverts any attempt to grow the button), so an image on the button can never
    reach the screen edge. Its container, NSStatusBarContentView, IS the full 30pt —
    so the tab lives here instead, underneath the button, and runs edge to edge.
    """

    def initWithFrame_(self, frame):
        self = objc.super(_StatusBackdrop, self).initWithFrame_(frame)
        if self is None:
            return None
        self._active = False
        return self

    def setActive_(self, active):
        active = bool(active)
        if active != self._active:
            self._active = active
            self.setNeedsDisplay_(True)

    def hitTest_(self, point):
        return None          # purely decorative: never swallow a click

    def drawRect_(self, dirty):
        try:
            if not self._active:
                return
            b = self.bounds()
            ctx = AppKit.NSGraphicsContext.currentContext()
            ctx.saveGraphicsState()
            if self.isFlipped():
                # _tab_path is written for y-up; flip so the flare lands at the edge.
                t = AppKit.NSAffineTransform.transform()
                t.translateXBy_yBy_(0.0, b.size.height)
                t.scaleXBy_yBy_(1.0, -1.0)
                t.concat()
            STATUS_BG().setFill()
            _tab_path(b.size.width, b.size.height).fill()
            ctx.restoreGraphicsState()
        except Exception:
            pass


# --------------------------------------------------------------------------- monitor
class MenuBarMonitor(AppKit.NSObject):
    def initWithOnOpen_(self, on_open):
        self = objc.super(MenuBarMonitor, self).init()
        if self is None:
            return None
        self._on_open = on_open

        bar = AppKit.NSStatusBar.systemStatusBar()
        self._item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        button = self._item.button()
        # The whole item is one image we draw ourselves, so switch off the system's
        # grey highlight capsule (drawn while the popover is open) — it would sit
        # behind the black tab and round off its square top.
        button.setImagePosition_(AppKit.NSImageOnly)
        try:
            cell = button.cell()
            cell.setHighlightsBy_(0)
            cell.setShowsStateBy_(0)
        except Exception:
            pass
        # No setMenu_: the button drives the popover itself.
        button.setTarget_(self)
        button.setAction_("togglePopover:")

        status = self._status()
        # Every one of these MUST be an instance attribute. A local would be released
        # the moment init returns and the popover would silently never appear.
        self._card = PlanCardView.alloc().initWithStatus_(status)
        self._open_btn = self._footer_button("Open Plan Details", "openPlan:", False)
        self._refresh_btn = self._footer_button("Refresh", "refreshNow:", True)
        self._card.addSubview_(self._open_btn)
        self._card.addSubview_(self._refresh_btn)

        self._vc = AppKit.NSViewController.alloc().init()
        self._vc.setView_(self._card)

        self._popover = AppKit.NSPopover.alloc().init()
        self._popover.setContentViewController_(self._vc)
        self._popover.setBehavior_(AppKit.NSPopoverBehaviorTransient)
        # Pin dark: the card is a fixed dark surface, so the beak and shadow must
        # match it even when the system is in Light Mode.
        self._popover.setAppearance_(
            AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
        # A transient popover dismisses itself on the mouse-DOWN that precedes this
        # button's action, so a naive toggle would close and instantly reopen it.
        # popoverDidClose_ stamps the time; togglePopover_ ignores a reopen inside
        # the same click.
        self._closed_at = 0.0
        self._popover.setDelegate_(self)
        self._card.setAppearance_(
            AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))

        self._layout_footer()
        self._last_status = status
        self._render_button(status)
        return self

    # ------------------------------------------------------------------ helpers
    @objc.python_method
    def _footer_button(self, title, action, secondary):
        b = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 10, 22))
        b.setBordered_(False)
        b.setButtonType_(AppKit.NSButtonTypeMomentaryChange)
        b.setTitle_(title)
        b.setAttributedTitle_(_attr(
            title, _font(13, AppKit.NSFontWeightMedium),
            TEXT_SECONDARY() if secondary else TEXT_LABEL()))
        b.setTarget_(self)
        b.setAction_(action)
        b.setFocusRingType_(AppKit.NSFocusRingTypeNone)
        # The popover window is never key, so without this the first click while
        # another app is frontmost is eaten as the activating click.
        try:
            b.setAcceptsFirstMouse_(True)
        except AttributeError:
            pass
        return b

    @objc.python_method
    def _layout_footer(self):
        h = self._card.frame().size.height
        top = h - FOOTER_H + 11
        ow = self._open_btn.attributedTitle().size().width + 4
        rw = self._refresh_btn.attributedTitle().size().width + 4
        self._open_btn.setFrame_(AppKit.NSMakeRect(PAD, top, ow, 20))
        self._refresh_btn.setFrame_(
            AppKit.NSMakeRect(CARD_W - PAD - rw, top, rw, 20))

    @objc.python_method
    def _status(self):
        try:
            return plan_mod.plan_status()
        except Exception as exc:
            print(f"plan status failed: {type(exc).__name__}: {exc}")
            return {"available": False, "source": "none"}

    # ------------------------------------------------------------------ update
    @objc.python_method
    def refresh(self):
        """Called from an NSTimer block every 30s -- must never raise."""
        try:
            status = self._status()
            self._last_status = status
            self._render_button(status)
            self._card.setStatus_(status)
            self._layout_footer()
            # Resize the open popover in place so a limit appearing or a window
            # resetting doesn't clip the card or leave a dead strip at the bottom.
            if self._popover.isShown():
                self._popover.setContentSize_(self._card.frame().size)
        except Exception as exc:
            print(f"menu bar refresh failed: {type(exc).__name__}: {exc}")

    @objc.python_method
    def _paint(self):
        """Redraw just the status item — used when the popover opens or closes, since
        the black tab is only worn while it is open."""
        try:
            self._render_button(self._last_status or self._status())
        except Exception as exc:
            print(f"status repaint failed: {type(exc).__name__}: {exc}")

    @objc.python_method
    def _headline(self, status):
        """The number the menu bar shows: the 5-hour session window by default.

        A maxed-out per-model weekly cap (a model you aren't actively using -- Fable at
        100% while you work in Opus) must not hijack the glanceable percentage, so only
        the 5-hour ``session`` and overall ``weekly_all`` limits are candidates. Escalate
        to weekly only when it is strictly more severe; per-model caps stay in the card.
        """
        first = {}
        for lim in status.get("limits") or []:
            first.setdefault(lim.get("kind"), lim)     # limits are severity-sorted
        session = first.get("session")
        weekly = first.get("weekly_all")
        headline = session or weekly or status.get("binding") or {}
        if session and weekly and (
            _SEVERITY_RANK.get(weekly.get("severity"), 0)
            > _SEVERITY_RANK.get(session.get("severity"), 0)
        ):
            headline = weekly
        return headline

    @objc.python_method
    def _render_button(self, status):
        button = self._item.button()
        thickness = AppKit.NSStatusBar.systemStatusBar().thickness()
        # The black tab is worn only while the popover is open.
        try:
            tab = bool(self._popover.isShown())
        except Exception:
            tab = False
        full = self._ensure_backdrop()
        if full:
            self._backdrop.setActive_(tab)
        draw_tab = tab and not full          # image tab only without a backdrop
        if not status.get("available"):
            img = _status_image(0, TEXT_SECONDARY(), thickness, show_pct=False,
                                tab=tab, draw_bg=draw_tab)
            self._pin_length(img, draw_tab)
            button.setImage_(img or self._symbol(
                "gauge.with.dots.needle.bottom.0percent",
                AppKit.NSColor.secondaryLabelColor()))
            button.setTitle_("")
            button.setToolTip_("No plan-usage data yet — run Claude Code")
            return

        headline = self._headline(status)
        pct = int(headline.get("percent") or 0)
        sev = headline.get("severity") or "normal"
        _live, stale, _badge = _freshness(status)

        color = SEVERITY_NSCOLOR.get(sev, SEVERITY_NSCOLOR["normal"])()
        if stale:
            color = AppKit.NSColor.secondaryLabelColor()   # don't imply it's current

        img = _status_image(pct, color, thickness, tab=tab, draw_bg=draw_tab)
        self._pin_length(img, draw_tab)
        if img is not None:
            button.setImage_(img)
            button.setTitle_("")
        else:
            # Fall back to the plain symbol + text if drawing ever fails.
            sym = ("gauge.with.dots.needle.100percent" if pct >= 90
                   else "gauge.with.dots.needle.67percent" if pct >= 40
                   else "gauge.with.dots.needle.33percent")
            button.setImagePosition_(AppKit.NSImageLeft)
            button.setImage_(self._symbol(sym, color))
            button.setAttributedTitle_(
                AppKit.NSAttributedString.alloc().initWithString_attributes_(
                    f" {pct}%", {AppKit.NSForegroundColorAttributeName: color,
                                 AppKit.NSFontAttributeName:
                                     AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
                                         12, AppKit.NSFontWeightMedium)}))
        button.setToolTip_(f"{_limit_name_short(headline)}: {pct}%"
                           + ("  ·  snapshot is stale" if stale else ""))

    @objc.python_method
    def _ensure_backdrop(self):
        """Attach the backdrop once the status item's view tree exists (it does not
        at init). Returns True when the tab can be drawn full height."""
        if getattr(self, "_backdrop", None) is not None and self._backdrop.superview():
            return True
        try:
            button = self._item.button()
            mid = button.superview()
            content = mid.superview() if mid is not None else None
            if content is None:
                return False
            bd = _StatusBackdrop.alloc().initWithFrame_(content.bounds())
            bd.setAutoresizingMask_(
                AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
            content.addSubview_positioned_relativeTo_(bd, AppKit.NSWindowBelow, None)
            self._backdrop = bd
            return True
        except Exception:
            self._backdrop = None
            return False

    @objc.python_method
    def _pin_length(self, img, tab):
        """While the tab is worn the item must be exactly as wide as the image.

        With the default variable length macOS pads the item (measured: a 55.3pt
        image in a 71pt item) and centres the image, so the system's own grey
        highlight shows down both sides of the black. Pinning the width makes the
        black the whole item. Released back to variable when the tab comes off.
        """
        try:
            if tab and img is not None:
                self._item.setLength_(img.size().width)
            else:
                self._item.setLength_(AppKit.NSVariableStatusItemLength)
        except Exception:
            pass

    @objc.python_method
    def _symbol(self, name, color):
        img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            name, "plan usage")
        if img is None:
            return None
        cfg = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
            13, AppKit.NSFontWeightRegular)
        try:
            tinted = cfg.configurationByApplyingConfiguration_(
                AppKit.NSImageSymbolConfiguration.configurationWithHierarchicalColor_(color))
            out = img.imageWithSymbolConfiguration_(tinted)
            if out is not None:
                out.setTemplate_(False)
                return out
        except Exception:
            pass
        return img

    # ------------------------------------------------------------------ actions
    def popoverDidClose_(self, notification):
        self._closed_at = time.monotonic()
        self._paint()              # closed -> take the tab off

    def togglePopover_(self, sender):
        try:
            if self._popover.isShown():
                self._popover.close()
                self._closed_at = time.monotonic()
                self._paint()
                return
            if time.monotonic() - self._closed_at < 0.25:
                return          # this very click is what dismissed it
            self.refresh()                       # never open on a stale card
            button = self._item.button()
            self._popover.showRelativeToRect_ofView_preferredEdge_(
                button.bounds(), button, AppKit.NSRectEdgeMinY)
            self._paint()          # now shown -> put the tab on
            # Deliberately NOT activateIgnoringOtherApps_: activating the app pulls
            # Purser's main window in front of whatever you were working in every
            # time you glance at the meter. A transient popover installs its own
            # event monitor, so it still takes key focus and still dismisses on an
            # outside click without the app coming forward.
        except Exception as exc:
            print(f"popover toggle failed: {type(exc).__name__}: {exc}")

    def openPlan_(self, sender):
        try:
            self._popover.close()
            self._closed_at = time.monotonic()
        except Exception:
            pass
        if self._on_open:
            self._on_open()

    def refreshNow_(self, sender):
        try:
            from csm.usage_live import live as _live
            _live.refresh_now()
        except Exception:
            pass
        self.refresh()
