"""Glue between the AppKit shell, the background indexer, and the query layer.

Threading contract:
  * Indexer callbacks fire on the indexer thread -> anything touching AppKit is
    hopped to main with AppHelper.callAfter. Bridge.send_event already does this.
  * Bridge handlers run on the bridge worker thread (sqlite reads are safe there:
    db.connect() is per-thread and WAL allows a reader alongside the writer).
"""
from __future__ import annotations

import time

import AppKit
from PyObjCTools import AppHelper

from csm import actions, config, db, indexer, live, queries
from csm.usage_live import live as usage_live
from csm.ui.menubar import MenuBarMonitor

LIVE_POLL_SECONDS = 3.0
RESCAN_SECONDS = 60.0
PLAN_POLL_SECONDS = 30.0    # re-read the usage cache; picks up CLI refreshes while working


class Coordinator:
    def __init__(self, controller):
        self.controller = controller
        self._live_ids: set[str] = set()
        self._last_progress = 0.0
        self._menubar = None

        self.indexer = indexer.IndexerThread(
            on_progress=self._on_index_progress, on_done=self._on_index_done)

    # ------------------------------------------------------------------ startup
    def start(self):
        db.init()
        self.controller.register_handlers(self.handlers())
        self.indexer.start()
        self.refresh_sidebar()          # paint whatever is already indexed
        self.indexer.request()          # then bring it up to date

        self._menubar = MenuBarMonitor.alloc().initWithOnOpen_(self._open_plan)

        # Live usage: a background thread fetches from the OAuth endpoint; the menu-bar
        # timer just re-reads the in-memory result (never blocks on the network).
        usage_live.start()

        AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            LIVE_POLL_SECONDS, True, lambda t: self._poll_live())
        AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            RESCAN_SECONDS, True, lambda t: self.indexer.request())
        AppKit.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
            PLAN_POLL_SECONDS, True, lambda t: self._menubar and self._menubar.refresh())

    def _open_plan(self):
        try:
            self.controller.focus_window()
            self.controller.sidebar().select_kind("plan")
            self.controller.web().navigate("plan", {})
        except Exception as exc:
            print(f"open plan failed: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ indexer
    def _on_index_progress(self, done, total, title):
        now = time.time()
        # Throttle: a full pass indexes ~100 sessions in seconds and would otherwise
        # spam evaluateJavaScript far faster than the UI can paint.
        if done < total and now - self._last_progress < 0.25:
            return
        self._last_progress = now
        self._event("indexProgress", {"done": done, "total": total, "title": title})

    def _on_index_done(self, stats):
        self._event("indexComplete", stats)
        AppHelper.callAfter(self.refresh_sidebar)

    # ------------------------------------------------------------------ live
    def _poll_live(self):
        ids = set(live.live_ids())
        if ids != self._live_ids:
            self._live_ids = ids
            self._event("liveSessions", {"ids": sorted(ids)})
            self.refresh_sidebar()

    # ------------------------------------------------------------------ sidebar
    def refresh_sidebar(self):
        try:
            self.controller.sidebar().set_projects(queries.projects())
        except Exception as exc:
            print(f"sidebar refresh failed: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ bridge
    def _event(self, name, data):
        try:
            self.controller.web().bridge().send_event(name, data)
        except Exception:
            pass

    def handlers(self) -> dict:
        import os
        import sys

        def ping(**_):
            return {"python": sys.version.split()[0], "pid": os.getpid(),
                    "claudeRoot": str(config.CLAUDE_ROOT)}

        def reindex(**_):
            self.indexer.request()
            return {"queued": True}

        def getProjects(**_):
            return queries.projects()

        def getOverview(**_):
            return queries.overview()

        def getPlan(**_):
            usage_live.refresh_now()   # nudge a live fetch; result lands on next poll
            data = queries.plan()
            data["liveStatus"] = usage_live.status()   # for diagnosing fetch failures
            return data

        def getSessions(cwd=None, sort="recent"):
            return queries.sessions(cwd=cwd, sort=sort)

        def getSession(id=None, **_):
            return queries.session(id)

        def getStatus(**_):
            conn = db.connect()
            row = conn.execute("SELECT COUNT(*) n FROM sessions").fetchone()
            return {"sessions": row["n"], "costCaption": config.COST_CAPTION,
                    "lastScan": db.get_meta("last_scan_ts")}

        def search(q=None, cwd=None, **_):
            return queries.search(q or "", cwd=cwd)

        def getTranscript(id=None, start=None, limit=100, **_):
            return queries.transcript(id, start=start, limit=limit)

        def resume(id=None, **_):
            s = queries.session(id)
            return actions.resume_in_terminal(id, s["cwd"])

        def copyResumeCommand(id=None, **_):
            s = queries.session(id)
            cmd, _cwd, _missing = actions.resume_command(id, s["cwd"])
            return actions.copy_to_clipboard(cmd)

        def revealInFinder(id=None, **_):
            return actions.reveal_in_finder(queries.session(id)["path"])

        def getCleanupList(sort="size", **_):
            return queries.cleanup_list(sort=sort)

        def trashSessions(ids=None, **_):
            ids = list(dict.fromkeys(ids or []))   # dedupe, preserve order
            # Liveness is re-checked HERE, not trusted from the UI's snapshot: a
            # session can start between the list being rendered and the click.
            running = live.live_sessions()
            blocked = [i for i in ids if i in running]
            todo = [i for i in ids if i not in running]

            paths, sids_with_paths = [], {}
            for sid in todo:
                try:
                    s = queries.session(sid)
                    p_list = queries.session_paths(sid)
                    sids_with_paths[sid] = (s["totalBytes"], p_list)
                    paths.extend(p_list)
                except KeyError:
                    pass

            result = actions.trash_paths(paths)
            # Only count bytes for sessions that were actually moved to the Trash: a
            # failed assertion or already-missing file stays on disk, and counting its
            # size would overstate what cleanup freed. A session whose paths only
            # partly trashed keeps data on disk, so it counts as not freed.
            trashed = set(result["trashed"])
            freed = sum(total for total, p_list in sids_with_paths.values()
                        if trashed.issuperset(p_list))
            for sid in todo:
                indexer.drop_session(sid)
            AppHelper.callAfter(self.refresh_sidebar)

            return {"trashed": len(result["trashed"]), "failed": result["failed"],
                    "blocked": blocked, "sessions": len(todo), "bytesFreed": freed}

        return {
            "ping": ping,
            "reindex": reindex,
            "getProjects": getProjects,
            "getOverview": getOverview,
            "getPlan": getPlan,
            "getSessions": getSessions,
            "getSession": getSession,
            "getStatus": getStatus,
            "getTranscript": getTranscript,
            "search": search,
            "resume": resume,
            "getCleanupList": getCleanupList,
            # NSPasteboard / NSWorkspace / NSFileManager-trash are main-thread APIs.
            "copyResumeCommand": (copyResumeCommand, True),
            "revealInFinder": (revealInFinder, True),
            "trashSessions": (trashSessions, True),
        }
