"""WKWebView content pane: local file load, JS bridge wiring, appearance sync."""
from __future__ import annotations

import os
import sys

import AppKit
import objc
import WebKit
from Foundation import NSURL, NSObject
from PyObjCTools import AppHelper

from csm import config
from csm.ui.bridge import BRIDGE_NAME, Bridge

_APPEARANCE_KEYPATH = "effectiveAppearance"


def _accent_hex() -> str:
    try:
        c = AppKit.NSColor.controlAccentColor().colorUsingColorSpace_(
            AppKit.NSColorSpace.sRGBColorSpace()
        )
        return "#%02x%02x%02x" % (
            round(c.redComponent() * 255),
            round(c.greenComponent() * 255),
            round(c.blueComponent() * 255),
        )
    except Exception:
        return "#0a84ff"


def _is_dark() -> bool:
    try:
        name = AppKit.NSApp.effectiveAppearance().bestMatchFromAppearancesWithNames_(
            [AppKit.NSAppearanceNameAqua, AppKit.NSAppearanceNameDarkAqua]
        )
        return name == AppKit.NSAppearanceNameDarkAqua
    except Exception:
        return False


class WebViewController(AppKit.NSViewController):
    def init(self):
        self = objc.super(WebViewController, self).init()
        if self is None:
            return None
        self._bridge = None
        self._ready = False
        return self

    # ------------------------------------------------------------------ view setup
    def loadView(self):
        cfg = WebKit.WKWebViewConfiguration.alloc().init()

        # Right-click -> Inspect Element while developing the web pane.
        try:
            cfg.preferences().setValue_forKey_(True, "developerExtrasEnabled")
        except Exception:
            pass

        webview = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            AppKit.NSMakeRect(0, 0, 800, 600), cfg
        )
        webview.setNavigationDelegate_(self)

        self._bridge = Bridge.alloc().initWithWebView_(webview)
        cfg.userContentController().addScriptMessageHandler_name_(self._bridge, BRIDGE_NAME)

        self.setView_(webview)
        self._webview = webview

        AppKit.NSApp.addObserver_forKeyPath_options_context_(
            self, _APPEARANCE_KEYPATH, 0, None
        )

    def dealloc(self):
        try:
            AppKit.NSApp.removeObserver_forKeyPath_(self, _APPEARANCE_KEYPATH)
        except Exception:
            pass
        objc.super(WebViewController, self).dealloc()

    @objc.python_method
    def bridge(self) -> Bridge:
        return self._bridge

    @objc.python_method
    def load(self):
        index = config.WEB_DIR / "index.html"
        url = NSURL.fileURLWithPath_(str(index))
        base = NSURL.fileURLWithPath_(str(config.WEB_DIR))
        self._webview.loadFileURL_allowingReadAccessToURL_(url, base)

    # ------------------------------------------------------------------ appearance
    def observeValueForKeyPath_ofObject_change_context_(self, kp, obj, change, ctx):
        if kp == _APPEARANCE_KEYPATH:
            self.pushAppearance()

    def pushAppearance(self):
        if self._bridge and self._ready:
            self._bridge.send_event("appearance",
                                    {"dark": _is_dark(), "accent": _accent_hex()})

    # ------------------------------------------------------- navigation delegate
    def webView_decidePolicyForNavigationAction_decisionHandler_(
            self, webview, action, handler):
        """Pin the webview to its own page; route real links to the browser.

        Transcripts are full of URLs. Without this, one click would navigate the
        content pane away from the app and there would be no way back — there is no
        back button, because this is a window, not a browser. Only http(s) is opened
        externally; anything else (javascript:, file:, data:) is dropped.
        """
        url = action.request().URL()
        scheme = (url.scheme() or "").lower() if url else ""

        if scheme in ("http", "https"):
            AppKit.NSWorkspace.sharedWorkspace().openURL_(url)   # hand off to the browser
            handler(WebKit.WKNavigationActionPolicyCancel)
        elif scheme == "file":
            handler(WebKit.WKNavigationActionPolicyAllow)         # our own page
        else:
            handler(WebKit.WKNavigationActionPolicyCancel)        # javascript:, data:, …

    def webView_didFinishNavigation_(self, webview, nav):
        self._ready = True
        self.pushAppearance()
        self.onReady()
        self._maybe_eval()
        self._maybe_snapshot()

    def _maybe_eval(self):
        """CSM_EVAL_JS=<expr> evaluates inside the real page and prints the result.

        Lets the web layer be unit-tested in its actual environment (real WKWebView,
        real CSP, real DOM) rather than against a shim that might not share the
        behaviour that matters.
        """
        src = os.environ.get("CSM_EVAL_JS")
        if not src:
            return

        def done(result, error):
            if error is not None:
                print(f"EVAL ERROR: {error.localizedDescription()}")
            else:
                print(result if isinstance(result, str) else repr(result))
            sys.stdout.flush()
            if os.environ.get("CSM_EVAL_QUIT") == "1":
                AppKit.NSApp.terminate_(None)

        def run():
            # callAsyncJavaScript treats the source as a function body and awaits the
            # result, so tests can `await csm.call(...)`. evaluateJavaScript cannot
            # marshal a Promise and fails with "unsupported type".
            self._webview.callAsyncJavaScript_arguments_inFrame_inContentWorld_completionHandler_(
                src, None, None, WebKit.WKContentWorld.pageWorld(), done)

        AppHelper.callLater(float(os.environ.get("CSM_EVAL_DELAY", "1.0")), run)

    # ------------------------------------------------------------ dev snapshotting
    def _maybe_snapshot(self):
        """CSM_SNAPSHOT=<path> renders the web pane to a PNG and (optionally) quits.

        Capturing the window through the window server needs Screen Recording
        permission; WKWebView can snapshot itself with none. Dev-only escape hatch so
        the web pane can be verified without a human at the screen.
        """
        path = os.environ.get("CSM_SNAPSHOT")
        if not path:
            return
        delay = float(os.environ.get("CSM_SNAPSHOT_DELAY", "2.5"))
        quit_after = os.environ.get("CSM_SNAPSHOT_QUIT") == "1"

        # CSM_SNAPSHOT_JS drives the pane to a particular view before capturing.
        setup = os.environ.get("CSM_SNAPSHOT_JS")
        if setup:
            self._webview.evaluateJavaScript_completionHandler_(setup, None)

        def shoot():
            def done(image, error):
                if image is not None:
                    tiff = image.TIFFRepresentation()
                    rep = AppKit.NSBitmapImageRep.imageRepWithData_(tiff)
                    png = rep.representationUsingType_properties_(
                        AppKit.NSBitmapImageFileTypePNG, {})
                    png.writeToFile_atomically_(path, True)
                    print(f"snapshot -> {path}")
                else:
                    print(f"snapshot failed: {error}")
                if quit_after:
                    AppKit.NSApp.terminate_(None)

            cfg = WebKit.WKSnapshotConfiguration.alloc().init()
            self._webview.takeSnapshotWithConfiguration_completionHandler_(cfg, done)

        AppHelper.callLater(delay, shoot)

    @objc.python_method
    def set_on_ready(self, fn):
        self._on_ready = fn

    def onReady(self):
        fn = getattr(self, "_on_ready", None)
        if fn:
            fn()

    # ------------------------------------------------------------------ navigation
    @objc.python_method
    def navigate(self, view: str, params: dict | None = None):
        """Native shell -> web pane view change."""
        if self._bridge:
            self._bridge.send_event("navigate", {"view": view, "params": params or {}})
