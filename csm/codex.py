"""Codex CLI usage: weekly rate limit and token consumption.

Codex writes one JSONL "rollout" per session under
``~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl``. Every turn appends an
``event_msg`` of type ``token_count`` carrying both halves of what we want:

    payload.info.total_token_usage   cumulative tokens for that session
    payload.rate_limits.primary      {used_percent, window_minutes, resets_at}

Two things learned from the real files and worth keeping in mind:

  * The LAST record in a file often has ``primary: null`` — the limit block is not
    populated on every turn — so the newest record is not necessarily the newest
    *reading*. Scan backwards until a populated one turns up.
  * ``resets_at`` is unix SECONDS here, unlike Claude's ISO strings.

Only ``primary`` has ever been non-null in this corpus (weekly, 10080 minutes);
``secondary`` is read anyway so a second window would surface on its own.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

CODEX_ROOT = Path(os.environ.get("CSM_CODEX_ROOT", "~/.codex")).expanduser()
SESSIONS_DIR = CODEX_ROOT / "sessions"

TAIL_BYTES = 512 * 1024      # enough to hold many turns; avoids reading 40MB files
MAX_FILES = 24               # newest-first; a limit reading older than these is useless

# Codex reports a bare percentage, so severity is ours to define. Matches the bands
# the Claude side already uses on the card.
def _severity(pct: float) -> str:
    if pct >= 90:
        return "critical"
    if pct >= 75:
        return "warning"
    return "normal"


def _rollouts() -> list[Path]:
    try:
        files = [p for p in SESSIONS_DIR.rglob("rollout-*.jsonl") if p.is_file()]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:MAX_FILES]


def _tail_lines(path: Path, limit: int = TAIL_BYTES) -> list[str]:
    """Last `limit` bytes as whole lines (the first partial line is dropped)."""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
                f.readline()          # discard the partial line
            data = f.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()


def _windows_from(record: dict) -> tuple[list[dict], str | None]:
    rl = (record.get("payload") or {}).get("rate_limits") or {}
    out = []
    for key, kind in (("primary", "codex_primary"), ("secondary", "codex_secondary")):
        w = rl.get(key)
        if not isinstance(w, dict):
            continue
        pct = w.get("used_percent")
        if pct is None:
            continue
        mins = w.get("window_minutes") or 0
        label = ("weekly" if 10000 <= mins <= 10200
                 else "monthly" if mins >= 40000
                 else "daily" if 1200 <= mins <= 1560
                 else f"{round(mins / 60)}-hour" if mins else "usage")
        resets = w.get("resets_at")
        try:
            resets_iso = (datetime.fromtimestamp(resets, timezone.utc).astimezone().isoformat()
                          if isinstance(resets, (int, float)) else None)
        except (OverflowError, OSError, ValueError):
            resets_iso = None
        out.append({
            "kind": kind,
            "label": label,
            "percent": int(round(float(pct))),
            "severity": _severity(float(pct)),
            "scopeModel": None,
            "resetsAt": resets_iso,
            "reset": False,
            "active": key == "primary",
            "windowMinutes": mins,
        })
    return out, rl.get("plan_type")


def _last_token_usage(lines: list[str]) -> dict | None:
    for line in reversed(lines):
        if '"total_token_usage"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        usage = ((rec.get("payload") or {}).get("info") or {}).get("total_token_usage")
        if isinstance(usage, dict):
            return usage
    return None


def codex_status() -> dict:
    """Newest usable Codex reading, normalised like csm.plan.plan_status()."""
    files = _rollouts()
    if not files:
        return {"available": False, "source": "none"}

    limits: list[dict] = []
    plan_type = None
    stamp = None
    tokens_total = 0
    sessions_counted = 0

    for path in files:
        lines = _tail_lines(path)
        if not lines:
            continue

        usage = _last_token_usage(lines)
        if usage:
            tokens_total += int(usage.get("total_tokens") or 0)
            sessions_counted += 1

        if limits:
            continue                       # already have the newest reading
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            found, plan = _windows_from(rec)
            if found:
                limits, plan_type = found, plan
                ts = rec.get("timestamp")
                try:
                    stamp = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                break

    if not limits:
        return {"available": False, "source": "none"}

    age_hours = None
    if stamp is not None:
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - stamp).total_seconds() / 3600

    limits.sort(key=lambda l: -l["percent"])
    return {
        "available": True,
        "source": "cache",              # always a file snapshot; Codex has no live API here
        "planType": plan_type,
        "fetchedAt": stamp.astimezone().isoformat() if stamp else None,
        "ageHours": age_hours,
        "stale": age_hours is not None and age_hours > 24,
        "limits": limits,
        "tokensTotal": tokens_total,
        "sessions": sessions_counted,
    }


if __name__ == "__main__":
    st = codex_status()
    print(json.dumps(st, indent=2))
