"""Menu-bar plan-usage monitor: an NSStatusItem glanceable from any app.

Why this can be "live": Claude Code itself refreshes ~/.claude.json's
`cachedUsageUtilization` while you work, so re-reading it on a short timer reflects
fresh numbers during an active session. When you haven't used the CLI recently the
snapshot is old — the item shows that honestly (a hollow indicator + "as of …") rather
than presenting stale data as current.
"""
from __future__ import annotations

import AppKit
import objc

from csm import plan as plan_mod

SEVERITY_NSCOLOR = {
    "critical": lambda: AppKit.NSColor.systemRedColor(),
    "warning": lambda: AppKit.NSColor.systemOrangeColor(),
    "normal": lambda: AppKit.NSColor.systemGreenColor(),
}

_SEVERITY_RANK = {"critical": 3, "warning": 2, "normal": 1, None: 0}

# Dropdown group order: the 5-hour window leads, then the overall weekly, then any
# per-model weekly caps -- matching the headline the menu-bar number tracks.
_KIND_ORDER = {"session": 0, "weekly_all": 1, "weekly_scoped": 2}


def _limit_name(lim: dict) -> str:
    kind = lim.get("kind")
    base = ("5-hour session" if kind == "session"
            else "weekly" if kind in ("weekly_all", "weekly_scoped")
            else kind or "usage")
    scope = lim.get("scopeModel")
    return f"{base} ({scope})" if scope else base


def _bar(percent: int, width: int = 10) -> str:
    filled = round(min(100, max(0, percent)) / 100 * width)
    return "▓" * filled + "░" * (width - filled)


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

        self._menu = AppKit.NSMenu.alloc().init()
        self._menu.setAutoenablesItems_(False)
        self._item.setMenu_(self._menu)

        self.refresh()
        return self

    # ------------------------------------------------------------------ update
    @objc.python_method
    def refresh(self):
        status = plan_mod.plan_status()
        self._render_button(status)
        self._render_menu(status)

    @objc.python_method
    def _headline(self, status):
        """The number the menu bar shows: the 5-hour session window by default.

        A maxed-out per-model weekly cap (a model you aren't actively using -- Fable at
        100% while you work in Opus) must not hijack the glanceable percentage, so only
        the 5-hour ``session`` and overall ``weekly_all`` limits are candidates. Escalate
        to weekly only when it is strictly more severe; per-model caps stay in the
        dropdown.
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
        button.setToolTip_(f"{_limit_name(headline)}: {pct}%"
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

    @objc.python_method
    def _render_menu(self, status):
        self._menu.removeAllItems()

        def header(text):
            it = AppKit.NSMenuItem.alloc().init()
            it.setEnabled_(False)
            attr = AppKit.NSAttributedString.alloc().initWithString_attributes_(
                text, {AppKit.NSFontAttributeName:
                       AppKit.NSFont.systemFontOfSize_weight_(11, AppKit.NSFontWeightSemibold),
                       AppKit.NSForegroundColorAttributeName:
                       AppKit.NSColor.secondaryLabelColor()})
            it.setAttributedTitle_(attr)
            self._menu.addItem_(it)

        def row(text, color=None):
            it = AppKit.NSMenuItem.alloc().init()
            it.setEnabled_(False)
            attrs = {AppKit.NSFontAttributeName:
                     AppKit.NSFont.monospacedSystemFontOfSize_weight_(12,
                         AppKit.NSFontWeightRegular)}
            if color is not None:
                attrs[AppKit.NSForegroundColorAttributeName] = color
            it.setAttributedTitle_(
                AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs))
            self._menu.addItem_(it)

        if not status.get("available"):
            header("PLAN USAGE")
            row("  No data yet — run Claude Code")
        else:
            if status.get("source") == "live":
                header("PLAN USAGE · live")
            else:
                age = status.get("ageHours")
                when = ("just now" if age is not None and age < 1.5
                        else f"{round(age)}h ago" if age is not None and age < 48
                        else f"{round(age/24)}d ago" if age is not None
                        else "unknown")
                header(f"PLAN USAGE · cached, {when}")
            for lim in sorted(status["limits"], key=lambda l: (
                    _KIND_ORDER.get(l["kind"], 3), -int(l.get("percent") or 0))):
                scope = f" {lim['scopeModel']}" if lim["scopeModel"] else ""
                name = ("5-hour" if lim["kind"] == "session"
                        else "weekly" if lim["kind"] == "weekly_all"
                        else "weekly*" if lim["kind"] == "weekly_scoped"
                        else lim["kind"])
                if lim.get("reset"):
                    row(f"  {_bar(0)}    ·%  {name}{scope}  (reset)",
                        AppKit.NSColor.tertiaryLabelColor())
                    continue
                pct = int(lim["percent"])
                color = SEVERITY_NSCOLOR.get(lim["severity"], SEVERITY_NSCOLOR["normal"])()
                row(f"  {_bar(pct)}  {pct:>3}%  {name}{scope}", color)
            if status.get("stale"):
                row("  snapshot is stale — open Claude Code to refresh",
                    AppKit.NSColor.tertiaryLabelColor())

        self._menu.addItem_(AppKit.NSMenuItem.separatorItem())
        opn = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Open Plan Details", "openPlan:", "")
        opn.setTarget_(self)
        self._menu.addItem_(opn)
        ref = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Refresh", "refreshNow:", "")
        ref.setTarget_(self)
        self._menu.addItem_(ref)

    # ------------------------------------------------------------------ actions
    def openPlan_(self, sender):
        if self._on_open:
            self._on_open()

    def refreshNow_(self, sender):
        try:
            from csm.usage_live import live as _live
            _live.refresh_now()
        except Exception:
            pass
        self.refresh()
