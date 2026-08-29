"""Vibrancy sidebar: NSOutlineView in source-list style.

Nodes are NSObject subclasses rather than plain Python objects so that the outline
view's identity comparisons are stable across the PyObjC bridge.
"""
from __future__ import annotations

import AppKit
import objc
from Foundation import NSObject

CELL_ID = "csm.cell"
GROUP_ID = "csm.group"


class SidebarNode(NSObject):
    """A row in the source list.

    PyObjC turns every attribute of an NSObject subclass into a selector, so this
    class carries no @classmethod/@property helpers — use the `node()` factory below
    and plain instance attributes.
    """

    def initWithTitle_symbol_kind_payload_(self, title, symbol, kind, payload):
        self = objc.super(SidebarNode, self).init()
        if self is None:
            return None
        self.title = title
        self.symbol = symbol
        self.kind = kind          # 'group' | 'overview' | 'cleanup' | 'project'
        self.payload = payload    # e.g. cwd for a project
        self.children = []
        self.badge = None         # count shown right-aligned
        self.live = False         # green dot
        self.is_group = (kind == "group")
        return self


def node(title, symbol=None, kind="item", payload=None) -> SidebarNode:
    return SidebarNode.alloc().initWithTitle_symbol_kind_payload_(
        title, symbol, kind, payload)


class SidebarViewController(AppKit.NSViewController):
    def init(self):
        self = objc.super(SidebarViewController, self).init()
        if self is None:
            return None
        self._on_select = None
        self._roots = []
        self._build_default_tree()
        return self

    @objc.python_method
    def _build_default_tree(self):
        library = node("Library", kind="group")
        library.children = [
            node("Overview", "chart.bar.xaxis", "overview"),
            node("Plan usage", "gauge.with.dots.needle.67percent", "plan"),
            node("Cleanup", "trash", "cleanup"),
        ]
        projects = node("Projects", kind="group")
        self._library = library
        self._projects = projects
        self._roots = [library, projects]

    # ------------------------------------------------------------------ view setup
    def loadView(self):
        scroll = AppKit.NSScrollView.alloc().initWithFrame_(
            AppKit.NSMakeRect(0, 0, 260, 600))
        scroll.setDrawsBackground_(False)
        scroll.setHasVerticalScroller_(True)
        scroll.setAutohidesScrollers_(True)

        outline = AppKit.NSOutlineView.alloc().initWithFrame_(scroll.bounds())
        outline.setHeaderView_(None)
        outline.setRowSizeStyle_(AppKit.NSTableViewRowSizeStyleDefault)
        outline.setFloatsGroupRows_(False)
        outline.setIndentationPerLevel_(12)
        outline.setBackgroundColor_(AppKit.NSColor.clearColor())
        try:
            outline.setStyle_(AppKit.NSTableViewStyleSourceList)
        except Exception:
            pass
        col = AppKit.NSTableColumn.alloc().initWithIdentifier_("main")
        col.setResizingMask_(AppKit.NSTableColumnAutoresizingMask)
        # NSTableColumn defaults to 100pt. Without an autoresizing style the column
        # never grows with the view, so every item cell is laid out 100pt wide inside
        # a ~240pt sidebar and its label truncates to nothing. Group rows are immune
        # (AppKit spans them across the full row), which is why only the headers were
        # legible. Uniform style makes the single column fill the outline view.
        col.setMinWidth_(80)
        col.setWidth_(240)
        outline.addTableColumn_(col)
        outline.setOutlineTableColumn_(col)
        outline.setColumnAutoresizingStyle_(
            AppKit.NSTableViewUniformColumnAutoresizingStyle)
        outline.setAutoresizingMask_(AppKit.NSViewWidthSizable)
        outline.setDataSource_(self)
        outline.setDelegate_(self)
        outline.setAutosaveName_("CSMSidebar")
        outline.setAutosaveExpandedItems_(True)

        scroll.setDocumentView_(outline)
        self._outline = outline
        self.setView_(scroll)

        outline.reloadData()
        for node in self._roots:
            outline.expandItem_(node)
        self.select_kind("overview")

    # ------------------------------------------------------------------ public API
    @objc.python_method
    def set_on_select(self, fn):
        self._on_select = fn

    @objc.python_method
    def set_projects(self, projects: list[dict]):
        """projects: [{'name':.., 'cwd':.., 'count':int, 'live':bool}, ...]"""
        kids = []
        for p in projects:
            n = node(p["name"], "folder", "project", p["cwd"])
            n.badge = str(p.get("count", "")) or None
            n.live = bool(p.get("live"))
            kids.append(n)

        # reloadData() rebinds the selection to a row INDEX: the old nodes are gone,
        # so the highlighted row silently re-points at whatever lands there — the
        # outline shows "Cleanup" highlighted while Overview content is on screen,
        # and no selection-did-change fires to correct it. Capture the selection by
        # (kind, payload) and re-select the matching new node afterwards.
        prev = None
        row = self._outline.selectedRow()
        if row >= 0:
            item = self._outline.itemAtRow_(row)
            if item is not None and not item.is_group:
                prev = (item.kind, item.payload)

        self._projects.children = kids
        self._outline.reloadData()
        self._outline.expandItem_(self._projects)

        if prev is None:
            return
        for root in self._roots:
            for child in root.children:
                if (child.kind, child.payload) == prev:
                    new_row = self._outline.rowForItem_(child)
                    if new_row >= 0:
                        self._outline.selectRowIndexes_byExtendingSelection_(
                            AppKit.NSIndexSet.indexSetWithIndex_(new_row), False)
                        return

    @objc.python_method
    def select_kind(self, kind: str):
        for root in self._roots:
            for child in root.children:
                if child.kind == kind:
                    row = self._outline.rowForItem_(child)
                    if row >= 0:
                        self._outline.selectRowIndexes_byExtendingSelection_(
                            AppKit.NSIndexSet.indexSetWithIndex_(row), False)
                    return

    # ------------------------------------------------------------------ datasource
    def outlineView_numberOfChildrenOfItem_(self, ov, item):
        return len(self._roots) if item is None else len(item.children)

    def outlineView_child_ofItem_(self, ov, index, item):
        return self._roots[index] if item is None else item.children[index]

    def outlineView_isItemExpandable_(self, ov, item):
        return len(item.children) > 0

    # Required by setAutosaveExpandedItems_: map nodes to/from a stable key so the
    # expanded/collapsed state survives relaunch.
    def outlineView_persistentObjectForItem_(self, ov, item):
        return f"{item.kind}:{item.title}"

    def outlineView_itemForPersistentObject_(self, ov, key):
        for root in self._roots:
            if f"{root.kind}:{root.title}" == key:
                return root
            for child in root.children:
                if f"{child.kind}:{child.title}" == key:
                    return child
        return None

    # ------------------------------------------------------------------ delegate
    def outlineView_isGroupItem_(self, ov, item):
        return item.is_group

    def outlineView_shouldSelectItem_(self, ov, item):
        return not item.is_group

    def outlineView_heightOfRowByItem_(self, ov, item):
        return 28.0 if item.is_group else 26.0

    def outlineView_viewForTableColumn_item_(self, ov, col, item):
        if item.is_group:
            cell = ov.makeViewWithIdentifier_owner_(GROUP_ID, self)
            if cell is None:
                cell = AppKit.NSTableCellView.alloc().init()
                cell.setIdentifier_(GROUP_ID)
                tf = AppKit.NSTextField.labelWithString_("")
                tf.setTranslatesAutoresizingMaskIntoConstraints_(False)
                tf.setFont_(AppKit.NSFont.systemFontOfSize_weight_(11,
                            AppKit.NSFontWeightSemibold))
                tf.setTextColor_(AppKit.NSColor.secondaryLabelColor())
                cell.addSubview_(tf)
                cell.setTextField_(tf)
                AppKit.NSLayoutConstraint.activateConstraints_([
                    tf.leadingAnchor().constraintEqualToAnchor_constant_(
                        cell.leadingAnchor(), 4),
                    tf.centerYAnchor().constraintEqualToAnchor_constant_(
                        cell.centerYAnchor(), 3),
                ])
            cell.textField().setStringValue_(item.title.upper())
            return cell

        cell = ov.makeViewWithIdentifier_owner_(CELL_ID, self)
        if cell is None:
            cell = AppKit.NSTableCellView.alloc().init()
            cell.setIdentifier_(CELL_ID)

            iv = AppKit.NSImageView.alloc().init()
            iv.setTranslatesAutoresizingMaskIntoConstraints_(False)
            cell.addSubview_(iv)
            cell.setImageView_(iv)

            tf = AppKit.NSTextField.labelWithString_("")
            tf.setTranslatesAutoresizingMaskIntoConstraints_(False)
            tf.setFont_(AppKit.NSFont.systemFontOfSize_(13))
            tf.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
            tf.setUsesSingleLineMode_(True)
            # Let the label yield when the sidebar is narrow: at default compression
            # resistance (750) it would fight the badge's pinned trailing edge instead
            # of truncating.
            tf.setContentCompressionResistancePriority_forOrientation_(
                AppKit.NSLayoutPriorityDefaultLow, AppKit.NSLayoutConstraintOrientationHorizontal)
            tf.setContentHuggingPriority_forOrientation_(
                AppKit.NSLayoutPriorityDefaultLow, AppKit.NSLayoutConstraintOrientationHorizontal)
            cell.addSubview_(tf)
            cell.setTextField_(tf)

            badge = AppKit.NSTextField.labelWithString_("")
            badge.setTranslatesAutoresizingMaskIntoConstraints_(False)
            badge.setFont_(AppKit.NSFont.systemFontOfSize_(11))
            badge.setTextColor_(AppKit.NSColor.tertiaryLabelColor())
            badge.setIdentifier_("badge")
            cell.addSubview_(badge)

            AppKit.NSLayoutConstraint.activateConstraints_([
                iv.leadingAnchor().constraintEqualToAnchor_constant_(
                    cell.leadingAnchor(), 2),
                iv.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()),
                iv.widthAnchor().constraintEqualToConstant_(18),
                tf.leadingAnchor().constraintEqualToAnchor_constant_(
                    iv.trailingAnchor(), 6),
                tf.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()),
                # Pin the label's trailing edge to the badge so it truncates against it.
                tf.trailingAnchor().constraintLessThanOrEqualToAnchor_constant_(
                    badge.leadingAnchor(), -6),
                badge.trailingAnchor().constraintEqualToAnchor_constant_(
                    cell.trailingAnchor(), -6),
                badge.leadingAnchor().constraintGreaterThanOrEqualToAnchor_constant_(
                    cell.leadingAnchor(), 40),
                badge.centerYAnchor().constraintEqualToAnchor_(cell.centerYAnchor()),
            ])

        cell.textField().setStringValue_(item.title)
        if item.symbol:
            img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                item.symbol, item.title)
            cell.imageView().setImage_(img)
            cell.imageView().setContentTintColor_(
                AppKit.NSColor.systemGreenColor() if item.live
                else AppKit.NSColor.secondaryLabelColor())
        for sub in cell.subviews():
            if sub.identifier() == "badge":
                sub.setStringValue_(item.badge or "")
        return cell

    def outlineViewSelectionDidChange_(self, note):
        row = self._outline.selectedRow()
        if row < 0 or self._on_select is None:
            return
        item = self._outline.itemAtRow_(row)
        if item is not None and not item.is_group:
            self._on_select(item)
