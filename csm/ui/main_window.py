"""Main window: unified-toolbar NSWindow hosting NSSplitViewController.

Sidebar vibrancy comes from NSSplitViewItem.sidebarWithViewController_, which applies
the system sidebar material — the same one Finder/Mail use. Search is a real
NSSearchToolbarItem rather than an in-page field: that is the strongest single signal
that this is an actual Mac app and not a web page in a frame.
"""
from __future__ import annotations

import os

import AppKit
import objc

from csm import config
from csm.ui.sidebar import SidebarViewController
from csm.ui.webview import WebViewController

WINDOW_AUTOSAVE = "CSMMainWindow"

TB_SIDEBAR = AppKit.NSToolbarToggleSidebarItemIdentifier
TB_FLEX = AppKit.NSToolbarFlexibleSpaceItemIdentifier
TB_REFRESH = "csm.refresh"
TB_SEARCH = "csm.search"


class MainWindowController(AppKit.NSObject):
    def init(self):
        self = objc.super(MainWindowController, self).init()
        if self is None:
            return None

        self._sidebar = SidebarViewController.alloc().init()
        self._web = WebViewController.alloc().init()
        self._sidebar.set_on_select(self._sidebar_selected)

        split = AppKit.NSSplitViewController.alloc().init()
        sidebar_item = AppKit.NSSplitViewItem.sidebarWithViewController_(self._sidebar)
        sidebar_item.setMinimumThickness_(200)
        sidebar_item.setMaximumThickness_(340)
        try:
            sidebar_item.setAllowsFullHeightLayout_(True)
        except Exception:
            pass
        content_item = AppKit.NSSplitViewItem.splitViewItemWithViewController_(self._web)
        content_item.setMinimumThickness_(560)
        split.addSplitViewItem_(sidebar_item)
        split.addSplitViewItem_(content_item)
        self._split = split

        rect = AppKit.NSMakeRect(0, 0, 1180, 760)
        style = (
            AppKit.NSWindowStyleMaskTitled
            | AppKit.NSWindowStyleMaskClosable
            | AppKit.NSWindowStyleMaskMiniaturizable
            | AppKit.NSWindowStyleMaskResizable
            | AppKit.NSWindowStyleMaskFullSizeContentView
        )
        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, AppKit.NSBackingStoreBuffered, False
        )
        win.setTitle_(config.APP_NAME)
        win.setMinSize_(AppKit.NSMakeSize(880, 520))
        win.setContentViewController_(split)

        # CSM_WINDOW_SIZE=WxH forces an exact content size for capturing full-height
        # panes (README screenshots). It deliberately does NOT touch the autosave name,
        # so a screenshot run never overwrites the user's real remembered window frame.
        forced_size = None
        env_size = os.environ.get("CSM_WINDOW_SIZE", "")
        if "x" in env_size:
            try:
                fw, fh = (int(n) for n in env_size.lower().split("x", 1))
                forced_size = AppKit.NSMakeSize(fw, fh)
            except ValueError:
                forced_size = None

        if forced_size is not None:
            win.setContentSize_(forced_size)
            win.center()
        else:
            # Restore the remembered frame; only fall back to centering on first launch.
            # (Doing this after center() would let a stale frame silently win, and a
            # saved frame from a narrow portrait display clamps the window next launch.)
            if not win.setFrameUsingName_(WINDOW_AUTOSAVE):
                main_screen = AppKit.NSScreen.screens()[0]
                vf = main_screen.visibleFrame()
                w = min(1180, vf.size.width - 80)
                h = min(760, vf.size.height - 80)
                win.setFrame_display_(
                    AppKit.NSMakeRect(vf.origin.x + (vf.size.width - w) / 2,
                                      vf.origin.y + (vf.size.height - h) / 2, w, h),
                    False)
            win.setFrameAutosaveName_(WINDOW_AUTOSAVE)

        toolbar = AppKit.NSToolbar.alloc().initWithIdentifier_("csm.toolbar")
        toolbar.setDelegate_(self)
        toolbar.setDisplayMode_(AppKit.NSToolbarDisplayModeIconOnly)
        toolbar.setAllowsUserCustomization_(False)
        win.setToolbar_(toolbar)
        try:
            win.setToolbarStyle_(AppKit.NSWindowToolbarStyleUnified)
        except Exception:
            pass
        try:
            win.setTitlebarSeparatorStyle_(AppKit.NSTitlebarSeparatorStyleLine)
        except Exception:
            pass

        self._window = win
        self._toolbar = toolbar
        self._search_item = None
        return self

    # ------------------------------------------------------------------ lifecycle
    def show(self):
        self._window.makeKeyAndOrderFront_(None)
        self._web.load()

    @objc.python_method
    def register_handlers(self, mapping: dict):
        # Touching .view() forces loadView, which is where the bridge is created.
        self._web.view()
        self._web.bridge().register_all(mapping)

    @objc.python_method
    def snapshot_window(self, path: str):
        """Render the native view hierarchy to a PNG (dev only).

        cacheDisplayInRect draws AppKit views directly, so this needs no Screen
        Recording permission. The WKWebView renders out-of-process and comes out
        blank — that's fine, this exists to inspect the sidebar and toolbar.
        """
        if os.environ.get("CSM_DIAG"):
            ov = self._sidebar.view().documentView()
            sv = self._sidebar.view()
            print(f"  scrollview   : {sv.frame().size.width:.0f} x {sv.frame().size.height:.0f}")
            print(f"  outlineview  : {ov.frame().size.width:.0f} x {ov.frame().size.height:.0f}")
            for c in ov.tableColumns():
                print(f"  column {c.identifier()!r}: width={c.width():.0f} "
                      f"min={c.minWidth():.0f} max={c.maxWidth():.0f}")
            for row in range(min(6, ov.numberOfRows())):
                v = ov.viewAtColumn_row_makeIfNecessary_(0, row, False)
                if v is None:
                    print(f"  row {row}: <no view>"); continue
                tf = v.textField() if hasattr(v, "textField") else None
                s_ = tf.stringValue() if tf else "<no textField>"
                w = tf.frame().size.width if tf else -1
                print(f"  row {row}: cell_w={v.frame().size.width:.0f} "
                      f"tf_w={w:.0f} tf_x={tf.frame().origin.x if tf else -1:.0f} "
                      f"text={s_!r}")

        target = os.environ.get("CSM_SNAPSHOT_TARGET", "window")
        if target == "sidebar":
            # The outline view itself is an ordinary NSView; the vibrancy material
            # around it is composited by the window server and never cache-renders.
            view = self._sidebar.view().documentView() or self._sidebar.view()
        else:
            view = self._window.contentView()
        # Include the titlebar/toolbar area, not just the content rect.
        frame = self._window.frame()
        area = self._window.contentRectForFrameRect_(frame)
        rect = view.bounds()
        rep = view.bitmapImageRepForCachingDisplayInRect_(rect)
        view.cacheDisplayInRect_toBitmapImageRep_(rect, rep)
        png = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
        png.writeToFile_atomically_(path, True)
        print(f"window snapshot -> {path} ({int(rect.size.width)}x{int(rect.size.height)})")

    @objc.python_method
    def focus_window(self):
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    @objc.python_method
    def web(self):
        return self._web

    @objc.python_method
    def sidebar(self):
        return self._sidebar

    # ------------------------------------------------------------------ actions
    @objc.python_method
    def _sidebar_selected(self, node):
        if node.kind == "project":
            self._web.navigate("project", {"cwd": node.payload, "name": node.title})
        else:
            self._web.navigate(node.kind, {})

    def refreshClicked_(self, sender):
        self._web.navigate("reindex", {})

    def searchChanged_(self, sender):
        q = str(sender.stringValue()).strip()
        self._web.navigate("search" if q else "overview", {"q": q})

    def focusSearch_(self, sender):
        if self._search_item is not None:
            try:
                self._search_item.beginSearchInteraction()
            except Exception:
                self._window.makeFirstResponder_(self._search_item.searchField())

    # ------------------------------------------------------------------ toolbar
    def toolbarAllowedItemIdentifiers_(self, tb):
        return [TB_SIDEBAR, TB_FLEX, TB_REFRESH, TB_SEARCH]

    def toolbarDefaultItemIdentifiers_(self, tb):
        return [TB_SIDEBAR, TB_FLEX, TB_REFRESH, TB_SEARCH]

    def toolbar_itemForItemIdentifier_willBeInsertedIntoToolbar_(self, tb, ident, flag):
        if ident == TB_REFRESH:
            item = AppKit.NSToolbarItem.alloc().initWithItemIdentifier_(ident)
            item.setLabel_("Refresh")
            item.setToolTip_("Rescan sessions")
            item.setImage_(
                AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    "arrow.clockwise", "Refresh"))
            item.setTarget_(self)
            item.setAction_("refreshClicked:")
            return item
        if ident == TB_SEARCH:
            item = AppKit.NSSearchToolbarItem.alloc().initWithItemIdentifier_(ident)
            item.setResignsFirstResponderWithCancel_(True)
            field = item.searchField()
            field.setPlaceholderString_("Search transcripts")
            field.setTarget_(self)
            field.setAction_("searchChanged:")
            field.setSendsWholeSearchString_(False)
            field.setSendsSearchStringImmediately_(False)
            self._search_item = item
            return item
        return None
