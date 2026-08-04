#!/usr/bin/env python3
"""Purser — a native macOS session ledger for Claude Code.

Run:  .venv/bin/python app.py
"""
from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import AppKit
import objc
from PyObjCTools import AppHelper

from csm import config
from csm.ui.coordinator import Coordinator
from csm.ui.main_window import MainWindowController


def _claim_app_name() -> None:
    """Make the menu bar say 'Purser' instead of 'Python'.

    Unbundled scripts inherit the interpreter's CFBundleName. Mutating the info
    dictionary before the app finishes launching is the standard workaround; it is
    cosmetic only, so failure is non-fatal. py2app makes this unnecessary later.
    """
    try:
        info = AppKit.NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = config.APP_NAME
    except Exception:
        pass


def _build_menubar(app: AppKit.NSApplication) -> None:
    """Programmatic main menu.

    The Edit menu is not optional: without it, the standard cut/copy/paste/select-all
    responder chain is missing and ⌘C/⌘V/⌘A do nothing inside the WKWebView.
    """
    main = AppKit.NSMenu.alloc().init()

    def add_submenu(title: str) -> AppKit.NSMenu:
        item = AppKit.NSMenuItem.alloc().init()
        main.addItem_(item)
        menu = AppKit.NSMenu.alloc().initWithTitle_(title)
        main.setSubmenu_forItem_(menu, item)
        return menu

    def add(menu, title, action, key, mask=None):
        it = menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
        if mask is not None:
            it.setKeyEquivalentModifierMask_(mask)
        return it

    app_menu = add_submenu(config.APP_NAME)
    add(app_menu, f"About {config.APP_NAME}", "orderFrontStandardAboutPanel:", "")
    app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    add(app_menu, f"Hide {config.APP_NAME}", "hide:", "h")
    add(app_menu, "Hide Others", "hideOtherApplications:", "h",
        AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagOption)
    add(app_menu, "Show All", "unhideAllApplications:", "")
    app_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    add(app_menu, f"Quit {config.APP_NAME}", "terminate:", "q")

    edit_menu = add_submenu("Edit")
    add(edit_menu, "Undo", "undo:", "z")
    add(edit_menu, "Redo", "redo:", "Z")
    edit_menu.addItem_(AppKit.NSMenuItem.separatorItem())
    add(edit_menu, "Cut", "cut:", "x")
    add(edit_menu, "Copy", "copy:", "c")
    add(edit_menu, "Paste", "paste:", "v")
    add(edit_menu, "Select All", "selectAll:", "a")

    view_menu = add_submenu("View")
    add(view_menu, "Toggle Sidebar", "toggleSidebar:", "s",
        AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagControl)

    window_menu = add_submenu("Window")
    add(window_menu, "Minimize", "performMiniaturize:", "m")
    add(window_menu, "Zoom", "performZoom:", "")
    app.setWindowsMenu_(window_menu)

    app.setMainMenu_(main)


class AppDelegate(AppKit.NSObject):
    def initWithController_coordinator_(self, controller, coordinator):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self._controller = controller
        self._coordinator = coordinator
        return self

    def applicationDidFinishLaunching_(self, notification):
        self._controller.show()
        self._coordinator.start()

        shot = os.environ.get("CSM_SNAPSHOT_WINDOW")
        if shot:
            def go():
                self._controller.snapshot_window(shot)
                if os.environ.get("CSM_SNAPSHOT_QUIT") == "1":
                    AppKit.NSApp.terminate_(None)
            AppHelper.callLater(float(os.environ.get("CSM_SNAPSHOT_DELAY", "4")), go)

    def applicationDidBecomeActive_(self, notification):
        # Sessions change while the app is in the background (Claude Code is running
        # right now); catch up whenever the user comes back to the window.
        try:
            self._coordinator.indexer.request()
        except Exception:
            pass

    def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
        return True


def main() -> int:
    _claim_app_name()

    app = AppKit.NSApplication.sharedApplication()
    # .Regular = real GUI app: dock icon, menu bar, can become frontmost.
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    _build_menubar(app)

    controller = MainWindowController.alloc().init()
    coordinator = Coordinator(controller)

    delegate = AppDelegate.alloc().initWithController_coordinator_(controller, coordinator)
    app.setDelegate_(delegate)

    app.activateIgnoringOtherApps_(True)
    # Let ^C in the launching terminal kill the app during development.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    AppHelper.runEventLoop(installInterrupt=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
