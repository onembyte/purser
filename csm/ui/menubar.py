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


CARD_BG = lambda: _srgb(28, 28, 30)            # noqa: E731 - lazy, see module docstring
TRACK = lambda: _srgb(58, 58, 60)              # noqa: E731
TEXT_PRIMARY = lambda: _srgb(255, 255, 255)    # noqa: E731
TEXT_LABEL = lambda: _srgb(245, 245, 247)      # noqa: E731
TEXT_SECONDARY = lambda: _srgb(142, 142, 147)  # noqa: E731
TEXT_TERTIARY = lambda: _srgb(120, 120, 125)   # noqa: E731
HAIRLINE = lambda: _srgb(46, 46, 48)           # noqa: E731

# Severity -> (gradient start, gradient end). Keyed by the exact strings plan.py emits.
FILL = {
    "normal": (lambda: _srgb(48, 209, 88), lambda: _srgb(50, 215, 75)),
    "warning": (lambda: _srgb(255, 179, 64), lambda: _srgb(255, 107, 53)),
    "critical": (lambda: _srgb(255, 107, 53), lambda: _srgb(255, 59, 48)),
}
# A stale snapshot gets a flat grey meter: colour would imply the number is current.
FILL_STALE = lambda: _srgb(90, 90, 94)         # noqa: E731

CARD_W = 340.0
PAD = 16.0
BAR_H = 8.0
CARD_RADIUS = 14.0
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


def card_height(status: dict) -> float:
    if not status.get("available"):
        return PAD + HEADER_H + HEADER_GAP + EMPTY_H + FOOTER_GAP + FOOTER_H
    n = len(status.get("limits") or [])
    body = (n * BLOCK_H + max(0, n - 1) * BLOCK_GAP) if n else EMPTY_H
    stale = STALE_H if status.get("stale") else 0.0
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
            AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
                a(), b()).drawInRect_angle_(rect, 0.0)
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
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, CARD_RADIUS, CARD_RADIUS).addClip()
        CARD_BG().setFill()
        AppKit.NSBezierPath.fillRect_(bounds)

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
                AppKit.NSMakeRect(PAD, y + 1, 15, 15), AppKit.NSZeroRect,
                AppKit.NSCompositingOperationSourceOver, 1.0, True, None)
            ctx.restoreGraphicsState()
        _attr("Plan Usage", f_title, TEXT_PRIMARY()).drawAtPoint_(
            AppKit.NSMakePoint(PAD + 22, y))

        if st.get("available"):
            if st.get("source") == "live" and not st.get("stale"):
                dot, note = FILL["normal"][0](), "live"
            else:
                age = st.get("ageHours")
                dot = TEXT_SECONDARY()
                note = ("just now" if age is not None and age < 1.5
                        else f"{round(age)}h ago" if age is not None and age < 48
                        else f"{round(age / 24)}d ago" if age is not None else "cached")
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
            stale = bool(st.get("stale"))
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
        button.setImagePosition_(AppKit.NSImageLeft)
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
        if not status.get("available"):
            button.setImage_(self._symbol("gauge.with.dots.needle.bottom.0percent",
                                          AppKit.NSColor.secondaryLabelColor()))
            button.setTitle_("")
            button.setToolTip_("No plan-usage data yet — run Claude Code")
            return

        headline = self._headline(status)
        pct = int(headline.get("percent") or 0)
        sev = headline.get("severity") or "normal"
        stale = status.get("stale")

        color = SEVERITY_NSCOLOR.get(sev, SEVERITY_NSCOLOR["normal"])()
        if stale:
            color = AppKit.NSColor.secondaryLabelColor()   # don't imply it's current

        sym = ("gauge.with.dots.needle.100percent" if pct >= 90
               else "gauge.with.dots.needle.67percent" if pct >= 40
               else "gauge.with.dots.needle.33percent")
        button.setImage_(self._symbol(sym, color))

        title = AppKit.NSAttributedString.alloc().initWithString_attributes_(
            f" {pct}%", {AppKit.NSForegroundColorAttributeName: color,
                         AppKit.NSFontAttributeName:
                             AppKit.NSFont.monospacedDigitSystemFontOfSize_weight_(
                                 12, AppKit.NSFontWeightMedium)})
        button.setAttributedTitle_(title)
        button.setToolTip_(f"{_limit_name_short(headline)}: {pct}%"
                           + ("  ·  snapshot is stale" if stale else ""))

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

    def togglePopover_(self, sender):
        try:
            if self._popover.isShown():
                self._popover.close()
                self._closed_at = time.monotonic()
                return
            if time.monotonic() - self._closed_at < 0.25:
                return          # this very click is what dismissed it
            self.refresh()                       # never open on a stale card
            button = self._item.button()
            self._popover.showRelativeToRect_ofView_preferredEdge_(
                button.bounds(), button, AppKit.NSRectEdgeMinY)
            # A popover from a status item only takes key focus once the app is
            # frontmost; without this the first click outside won't dismiss it.
            AppKit.NSApp.activateIgnoringOtherApps_(True)
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
