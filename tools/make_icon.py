#!/usr/bin/env python3
"""Generate the app icon: `python tools/make_icon.py` -> assets/icon.icns

Drawn with Core Graphics rather than shipped as a binary asset, so the palette stays
tied to the one the charts use (csm/config.py / web/app.css --series-N).

Big Sur icon geometry: content sits inside a rounded square inset ~10% from the canvas,
corner radius ~22.4% of the square. Bars, not a glyph — legible down to 16px, and it is
what the app is actually about.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import AppKit
import Quartz
from Foundation import NSURL

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

# Slots 1/2/6 of the validated categorical palette (blue, green, orange), dark steps.
BARS = [(0x39, 0x87, 0xE5), (0x00, 0x83, 0x00), (0xD9, 0x59, 0x26)]


def rgb(ctx, r, g, b, a=1.0):
    Quartz.CGContextSetRGBFillColor(ctx, r / 255, g / 255, b / 255, a)


def rounded(ctx, x, y, w, h, r):
    path = Quartz.CGPathCreateMutable()
    Quartz.CGPathAddRoundedRect(path, None, Quartz.CGRectMake(x, y, w, h), r, r)
    Quartz.CGContextAddPath(ctx, path)


def bar_path(ctx, x, y, w, h, r):
    """Rounded top, square baseline — the same mark rule the charts use.

    (y grows upward in this context, so the 'top' is y + h.)
    """
    r = min(r, w / 2, h)
    p = Quartz.CGPathCreateMutable()
    Quartz.CGPathMoveToPoint(p, None, x, y)
    Quartz.CGPathAddLineToPoint(p, None, x, y + h - r)
    Quartz.CGPathAddQuadCurveToPoint(p, None, x, y + h, x + r, y + h)
    Quartz.CGPathAddLineToPoint(p, None, x + w - r, y + h)
    Quartz.CGPathAddQuadCurveToPoint(p, None, x + w, y + h, x + w, y + h - r)
    Quartz.CGPathAddLineToPoint(p, None, x + w, y)
    Quartz.CGPathCloseSubpath(p)
    Quartz.CGContextAddPath(ctx, p)


def draw(size: int) -> AppKit.NSBitmapImageRep:
    rep = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
    nsctx = AppKit.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    AppKit.NSGraphicsContext.saveGraphicsState()
    AppKit.NSGraphicsContext.setCurrentContext_(nsctx)
    ctx = nsctx.CGContext()

    S = float(size)
    inset = S * 0.094                       # Big Sur content inset
    side = S - inset * 2
    radius = side * 0.224

    # --- squircle body: graphite, subtly lighter at the top
    Quartz.CGContextSaveGState(ctx)
    rounded(ctx, inset, inset, side, side, radius)
    Quartz.CGContextClip(ctx)
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    grad = Quartz.CGGradientCreateWithColorComponents(
        space, (0.235, 0.235, 0.251, 1.0, 0.106, 0.106, 0.118, 1.0), (0.0, 1.0), 2)
    Quartz.CGContextDrawLinearGradient(
        ctx, grad, Quartz.CGPointMake(0, S), Quartz.CGPointMake(0, inset), 0)

    # --- ascending bars, baseline-anchored with rounded tops (same rule as the charts)
    n = len(BARS)
    gap = side * 0.085
    bw = (side * 0.62 - gap * (n - 1)) / n
    base = inset + side * 0.235
    left = inset + (side - (bw * n + gap * (n - 1))) / 2
    heights = [0.26, 0.42, 0.56]
    for i, ((r, g, b), hf) in enumerate(zip(BARS, heights)):
        x = left + i * (bw + gap)
        rgb(ctx, r, g, b)
        bar_path(ctx, x, base, bw, side * hf, bw * 0.36)
        Quartz.CGContextFillPath(ctx)

    # --- baseline rule
    rgb(ctx, 255, 255, 255, 0.34)
    rule_h = max(1.0, S * 0.011)
    Quartz.CGContextFillRect(
        ctx, Quartz.CGRectMake(left - side * 0.07, base - rule_h,
                               bw * n + gap * (n - 1) + side * 0.14, rule_h))
    Quartz.CGContextRestoreGState(ctx)

    # --- hairline so the icon reads on a light Dock
    rounded(ctx, inset, inset, side, side, radius)
    Quartz.CGContextSetRGBStrokeColor(ctx, 1, 1, 1, 0.10)
    Quartz.CGContextSetLineWidth(ctx, max(1.0, S * 0.004))
    Quartz.CGContextStrokePath(ctx)

    AppKit.NSGraphicsContext.restoreGraphicsState()
    return rep


def main() -> int:
    ASSETS.mkdir(exist_ok=True)
    iconset = ASSETS / "icon.iconset"
    iconset.mkdir(exist_ok=True)

    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = base * scale
            rep = draw(px)
            png = rep.representationUsingType_properties_(
                AppKit.NSBitmapImageFileTypePNG, {})
            name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
            png.writeToFile_atomically_(str(iconset / name), True)

    out = ASSETS / "icon.icns"
    r = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        return 1
    print(f"wrote {out} ({out.stat().st_size / 1024:.0f}KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
