"""JSON-RPC-flavoured bridge between the WKWebView (JS) and Python.

Wire protocol
-------------
  JS  -> Py : window.webkit.messageHandlers.csm.postMessage({id, method, params})
  Py  -> JS : window.__csm._reply({id, ok:true, result})
              window.__csm._reply({id, ok:false, error})
              window.__csm._event({event, data})

Threading
---------
`userContentController_didReceiveScriptMessage_` fires on the main thread. Handlers may
touch sqlite or walk the filesystem, so they run on a single worker thread and the reply
is marshalled back to the main thread (evaluateJavaScript is main-thread only).

Handlers needing AppKit (pasteboard, NSWorkspace) declare `main_thread=True` and run inline.
"""
from __future__ import annotations

import json
import queue
import threading
import traceback
from typing import Any, Callable

import objc
import WebKit
from Foundation import NSObject
from PyObjCTools import AppHelper

BRIDGE_NAME = "csm"


class Bridge(NSObject):
    """Implements WKScriptMessageHandler."""

    def initWithWebView_(self, webview):
        self = objc.super(Bridge, self).init()
        if self is None:
            return None
        self._webview = webview
        self._handlers: dict[str, tuple[Callable[..., Any], bool]] = {}
        self._q: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._run_worker, name="csm-bridge",
                                        daemon=True)
        self._worker.start()
        return self

    # ------------------------------------------------------------------ registration
    @objc.python_method
    def register(self, name: str, fn: Callable[..., Any], main_thread: bool = False) -> None:
        self._handlers[name] = (fn, main_thread)

    @objc.python_method
    def register_all(self, mapping: dict) -> None:
        """Values are `fn` or `(fn, main_thread)` — AppKit-touching handlers need the latter."""
        for name, entry in mapping.items():
            if isinstance(entry, tuple):
                self.register(name, entry[0], entry[1])
            else:
                self.register(name, entry)

    # ------------------------------------------------------------- inbound from JS
    def userContentController_didReceiveScriptMessage_(self, controller, message):
        body = message.body()
        try:
            msg = json.loads(body) if isinstance(body, str) else dict(body)
        except Exception:
            return
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        entry = self._handlers.get(method)
        if entry is None:
            self._reply(mid, False, f"unknown method: {method}")
            return
        fn, main_thread = entry
        if main_thread:
            self._invoke(mid, fn, params)
        else:
            self._q.put((mid, fn, params))

    @objc.python_method
    def _run_worker(self) -> None:
        while True:
            mid, fn, params = self._q.get()
            self._invoke(mid, fn, params)

    @objc.python_method
    def _invoke(self, mid, fn, params) -> None:
        try:
            result = fn(**params) if params else fn()
            self._reply(mid, True, result)
        except Exception as exc:
            traceback.print_exc()
            self._reply(mid, False, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------- outbound to JS
    @objc.python_method
    def _reply(self, mid, ok: bool, payload) -> None:
        env = {"id": mid, "ok": ok}
        env["result" if ok else "error"] = payload
        self._eval(f"window.__csm._reply({json.dumps(env)})")

    @objc.python_method
    def send_event(self, event: str, data: Any = None) -> None:
        self._eval(f"window.__csm._event({json.dumps({'event': event, 'data': data})})")

    @objc.python_method
    def _eval(self, js: str) -> None:
        # json.dumps(ensure_ascii=True) keeps U+2028/2029 and non-BMP chars escaped,
        # so the payload is always a safe JS literal.
        def run():
            self._webview.evaluateJavaScript_completionHandler_(js, None)
        AppHelper.callAfter(run)
