#!/usr/bin/env python3
"""Render the menu-bar plan card to PNGs *without* opening a popover.

Dev-only visual check for the glass restyle. PlanCardView draws itself with
cacheDisplayInRect, which needs no NSApplication, no window and no Screen Recording
permission. Each fixture below exercises one layout branch of the card so a restyle
can be eyeballed (or diffed) at a glance:

  /tmp/purser-card-live.png      live snapshot, three limits (the common case)
  /tmp/purser-card-stale.png     stale snapshot, grey meters + footer note
  /tmp/purser-card-empty.png     no plan data yet
  /tmp/purser-card-reset.png     the 5-hour window mid-countdown ("Window reset")
  /tmp/purser-ring-*.png         the status-item progress ring at several percentages

Run:  .venv/bin/python tools/render_card.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import AppKit

from csm.ui.menubar import PlanCardView, _ring_image


# --------------------------------------------------------------------------- fixtures
def _iso_in(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


FIXTURES = {
    "live": {
        "available": True, "source": "live", "stale": False, "ageHours": 0.1,
        "limits": [
            {"kind": "session", "percent": 73, "severity": "warning",
             "resetsAt": _iso_in(0.85), "reset": False},
            {"kind": "weekly_all", "percent": 7, "severity": "normal",
             "resetsAt": _iso_in(20), "reset": False},
            {"kind": "weekly_scoped", "percent": 52, "severity": "normal",
             "scopeModel": "Fable", "resetsAt": _iso_in(20), "reset": False},
        ],
    },
    "stale": {
        "available": True, "source": "cache", "stale": True, "ageHours": 9.2,
        "limits": [
            {"kind": "session", "percent": 100, "severity": "critical",
             "resetsAt": _iso_in(-1), "reset": True},
            {"kind": "weekly_all", "percent": 34, "severity": "normal",
             "resetsAt": _iso_in(20), "reset": False},
        ],
    },
    "empty": {"available": False, "source": "none"},
    "reset": {
        "available": True, "source": "live", "stale": False, "ageHours": 0.05,
        "limits": [
            {"kind": "session", "percent": 0, "severity": "normal",
             "resetsAt": _iso_in(4.5), "reset": True},
            {"kind": "weekly_all", "percent": 41, "severity": "warning",
             "resetsAt": _iso_in(60), "reset": False},
        ],
    },
    "zero": {
        # Exercises the ring's pct=0 path in situ: track visible, no arc.
        "available": True, "source": "live", "stale": False, "ageHours": 0.1,
        "limits": [
            {"kind": "session", "percent": 0, "severity": "normal",
             "resetsAt": _iso_in(5), "reset": False},
            {"kind": "weekly_all", "percent": 0, "severity": "normal",
             "resetsAt": _iso_in(100), "reset": False},
        ],
    },
}


def render(name: str, status: dict) -> None:
    view = PlanCardView.alloc().initWithStatus_(status)
    rect = view.bounds()
    rep = view.bitmapImageRepForCachingDisplayInRect_(rect)
    view.cacheDisplayInRect_toBitmapImageRep_(rect, rep)
    png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    out = f"/tmp/purser-card-{name}.png"
    png.writeToFile_atomically_(out, True)
    print(out, int(rect.size.width), "x", int(rect.size.height))


def render_rings() -> None:
    """Draw the status-item ring standalone scaled up, since the track uses
    labelColour and must adapt to both menu-bar appearances."""
    cases = [("0", 0, AppKit.NSColor.systemGreenColor()),
             ("40", 40, AppKit.NSColor.systemGreenColor()),
             ("73", 73, AppKit.NSColor.systemOrangeColor()),
             ("95", 95, AppKit.NSColor.systemRedColor())]
    for tag, pct, color in cases:
        img = _ring_image(pct, color)
        if img is None:
            print(f"ring {tag}: FAILED to build")
            continue
        size = 96
        rep = AppKit.NSBitmapImageRep.alloc().\
            initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
                None, size * 2, size * 2, 8, 4, True, False,
                AppKit.NSCalibratedRGBColorSpace, 0, 0)
        rep.setSize_(AppKit.NSMakeSize(size, size))
        ctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        prev = AppKit.NSGraphicsContext.currentContext()
        AppKit.NSGraphicsContext.setCurrentContext_(ctx)
        try:
            # A menu-bar-coloured ground, since the track is 25% labelColour.
            AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.15, 0.15, 0.16, 1.0).setFill()
            AppKit.NSBezierPath.fillRect_(AppKit.NSMakeRect(0, 0, size, size))
            img.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                AppKit.NSMakeRect(24, 24, 48, 48), AppKit.NSZeroRect,
                AppKit.NSCompositingOperationSourceOver, 1.0, True, None)
        finally:
            AppKit.NSGraphicsContext.setCurrentContext_(prev)
        out = f"/tmp/purser-ring-{tag}.png"
        rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}
                                                ).writeToFile_atomically_(out, True)
        print(out)


if __name__ == "__main__":
    for name, status in FIXTURES.items():
        render(name, status)
    render_rings()