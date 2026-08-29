"""Live session registry.

Claude Code writes ~/.claude/sessions/<pid>.json while a session is running:
    {"pid":53998,"sessionId":"...","cwd":"...","startedAt":...,"version":"2.1.208",
     "kind":"interactive","status":"waiting","waitingFor":"dialog open","updatedAt":...}

Entries are not always cleaned up on crash, so a record only counts as live if the pid
is actually alive. This gates the cleanup feature: a live session is never trashable.
"""
from __future__ import annotations

import json
import os

from csm import config


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else
    except (OverflowError, ValueError, TypeError):
        return False
    return True


def live_sessions() -> dict[str, dict]:
    """-> {session_id: {pid, cwd, status, waiting_for, version}} for running sessions."""
    out: dict[str, dict] = {}
    if not config.LIVE_DIR.is_dir():
        return out
    try:
        entries = list(config.LIVE_DIR.iterdir())
    except OSError:
        return out

    for f in entries:
        if f.suffix != ".json":
            continue
        try:
            # explicit utf-8 (not read_text's locale default) — a py2app bundle's
            # default encoding differs from the shell's; see csm/plan._load.
            rec = json.loads(f.read_bytes().decode("utf-8"))
        except (OSError, ValueError):
            continue
        sid = rec.get("sessionId")
        pid = rec.get("pid")
        if not sid or not isinstance(pid, int):
            continue

        # Only pid liveness may exclude a record here. The old 24h-updatedAt gate was
        # a pid-reuse defence, but it excluded merely IDLE live processes — a session
        # parked at a dialog over a weekend stopped being locked in Cleanup while the
        # process kept appending to its files, so trashing it corrupted a running
        # session. Asymmetry of cost decides: a recycled-pid phantom costs one
        # untrashable row nobody will ever miss; a wrong exclusion costs transcript
        # data. When in doubt, lock.
        if not _pid_alive(pid):
            continue

        out[sid] = {
            "pid": pid,
            "cwd": rec.get("cwd"),
            "status": rec.get("status"),
            "waiting_for": rec.get("waitingFor"),
            "version": rec.get("version"),
            "kind": rec.get("kind"),
        }
    return out


def live_ids() -> list[str]:
    return list(live_sessions().keys())
