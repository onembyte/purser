"""Real subscription-plan usage.

Claude Code caches Anthropic's own utilization report in ~/.claude.json under
`cachedUsageUtilization`, and daily activity in ~/.claude/stats-cache.json. This reads
both. Two things to keep honest about:

  * Plan limits are reported as PERCENT of rolling windows (5-hour session + weekly,
    plus per-model weekly scopes). Anthropic does NOT expose the dollar/token size of a
    window (`limit_dollars` is null), so there is no true "dollars of plan" figure — a
    per-session plan share can only be a share of the user's own measured usage.
  * The snapshot is only as fresh as the last time the CLI fetched it. It is always
    stamped with its age; a stale one is shown as "last known", never as "current".

This file reads the CLI's private state defensively — every field via .get(), any shape
change degrades to "unavailable" rather than raising.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

# These live at the account root, NOT under $CSM_CLAUDE_ROOT (which may point at a
# fixture copy). Usage is per-account, so always read the real home.
CLAUDE_JSON = Path("~/.claude.json").expanduser()
STATS_CACHE = Path("~/.claude/stats-cache.json").expanduser()

SEVERITY_ORDER = {"critical": 3, "warning": 2, "normal": 1, None: 0}

# Claude Code rewrites ~/.claude.json (~100KB) while it runs, and a read can catch it
# mid-write. Keep the last good parse per path so a transient failure never blanks the
# live monitor — a one-off race returns the previous value instead of "no data".
_last_good: dict[str, dict] = {}


def _load(path: Path) -> dict | None:
    key = str(path)
    for attempt in range(2):
        try:
            # read_bytes + explicit utf-8 is locale-immune. A py2app app bundle runs
            # with a different default text encoding than the shell, so read_text()
            # (which uses the locale encoding) raises UnicodeDecodeError on this file's
            # non-ASCII content inside the packaged app while working fine unbundled.
            data = json.loads(path.read_bytes().decode("utf-8"))
            _last_good[key] = data
            return data
        except ValueError:
            if attempt == 0:
                time.sleep(0.05)      # likely a partial write in progress; try once more
                continue
            return _last_good.get(key)   # fall back to the last complete read
        except OSError:
            return _last_good.get(key)
    return _last_good.get(key)


def _iso(ts: str | None) -> str | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).astimezone().isoformat()
    except Exception:
        return ts


def plan_status() -> dict:
    """Anthropic's utilization report, normalized.

    Prefers live data fetched from the OAuth endpoint (see usage_live); falls back to
    the ~/.claude.json snapshot when live is unavailable. `source` says which was used.
    """
    # Lazy import avoids a hard dependency cycle and lets plan.py be used in tests
    # without the live fetcher running.
    try:
        from csm.usage_live import live as _live
        got = _live.latest()
    except Exception:
        got = None

    if got is not None:
        util, fetched_ms = got
        return _build_status(util, fetched_ms, source="live")

    root = _load(CLAUDE_JSON) or {}
    cached = root.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return {"available": False, "source": "none"}
    return _build_status(cached.get("utilization") or {},
                         cached.get("fetchedAtMs"), source="cache")


def _build_status(util: dict, fetched_ms, source: str) -> dict:
    fetched = None
    age_hours = None
    if isinstance(fetched_ms, (int, float)):
        fetched = datetime.fromtimestamp(fetched_ms / 1000, timezone.utc)
        age_hours = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600

    now = datetime.now(timezone.utc)
    util = util or {}
    limits = []
    for lim in util.get("limits") or []:
        if not isinstance(lim, dict):
            continue
        scope = lim.get("scope") or {}
        model = (scope.get("model") or {}).get("display_name") if scope else None

        # A limit whose reset time is in the PAST has already rolled over — the cached
        # percentage is meaningless (the real value is ~0). The 5-hour window is fast
        # and resets often, so even a 2-hour-old snapshot can show a bogus "100%" for it
        # while the slow weekly windows still read correctly. Flag it rather than alarm.
        reset = False
        try:
            ra = lim.get("resets_at")
            if ra and datetime.fromisoformat(ra) < now:
                reset = True
        except Exception:
            pass

        limits.append({
            "kind": lim.get("kind"),
            "group": lim.get("group"),
            "percent": 0 if reset else (lim.get("percent") or 0),
            "reportedPercent": lim.get("percent") or 0,
            "severity": "normal" if reset else (lim.get("severity") or "normal"),
            "scopeModel": model,
            "resetsAt": _iso(lim.get("resets_at")),
            "reset": reset,
            "active": bool(lim.get("is_active")) and not reset,
        })
    # Most severe first so the binding constraint leads.
    limits.sort(key=lambda l: (SEVERITY_ORDER.get(l["severity"], 0), l["percent"]),
                reverse=True)

    extra = util.get("extra_usage") or {}
    # The binding constraint is the worst limit that is genuinely current — a reset
    # window is never "binding" even if its stale percent was high.
    live_limits = [l for l in limits if not l["reset"]]
    binding = next((l for l in live_limits if l["active"]), None) or \
        (live_limits[0] if live_limits else (limits[0] if limits else None))

    return {
        "available": True,
        "source": source,           # "live" | "cache"
        "fetchedAt": fetched.astimezone().isoformat() if fetched else None,
        "ageHours": age_hours,
        # Live data is current by definition; only the file cache can be stale. A single
        # fast window having rolled over is handled per-limit via `reset`.
        "stale": source == "cache" and age_hours is not None and age_hours > 6,
        "hasResetWindow": any(l["reset"] for l in limits),
        "limits": limits,
        "binding": binding,
        "extraCredits": {
            "enabled": bool(extra.get("is_enabled")),
            "reason": extra.get("disabled_reason"),
            "usedCents": extra.get("used_credits"),
        },
        # Anthropic exposes percent only; make the absence explicit for the UI.
        "dollarsKnown": False,
    }


def daily_activity() -> list[dict]:
    """Per-day message/session/tool counts the CLI itself computed (a real cross-check)."""
    data = _load(STATS_CACHE) or {}
    out = []
    for row in data.get("dailyActivity") or []:
        if isinstance(row, dict) and row.get("date"):
            out.append({
                "date": row["date"],
                "messages": row.get("messageCount") or 0,
                "sessions": row.get("sessionCount") or 0,
                "toolCalls": row.get("toolCallCount") or 0,
            })
    out.sort(key=lambda r: r["date"])
    return out
