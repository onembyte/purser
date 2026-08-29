"""Read API over the index. Everything the web pane renders comes from here.

All aggregates are precomputed by the indexer, so these are plain SQL reads — no
parsing happens on the request path except `transcript()`, which seeks into the
canonical .jsonl by byte offset.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from csm import config, db, live, plan as plan_mod

SESSION_COLS = """
  session_id, title, title_source, cwd, git_branch, first_ts, last_ts, cli_version,
  human_msgs, assistant_msgs, total_records, malformed_lines, models, estimated,
  cost_usd, in_tok, out_tok, cache_w_tok, cache_r_tok, file_bytes, extra_bytes,
  canonical_path, dup_paths, slug
"""


def _session_dict(row, live_map: dict | None = None) -> dict:
    models = json.loads(row["models"] or "{}")
    # Rank models by record count; the UI shows the dominant one as a chip.
    ranked = sorted(models.items(), key=lambda kv: -kv[1])
    d = {
        "id": row["session_id"],
        "title": row["title"],
        "titleSource": row["title_source"],
        "cwd": row["cwd"],
        "project": os.path.basename(row["cwd"]) if row["cwd"] else "—",
        "branch": row["git_branch"],
        "firstTs": row["first_ts"],
        "lastTs": row["last_ts"],
        "cliVersion": row["cli_version"],
        "humanMsgs": row["human_msgs"],
        "assistantMsgs": row["assistant_msgs"],
        "totalRecords": row["total_records"],
        "malformed": row["malformed_lines"],
        "models": [{"id": m, "name": config.display_name(m), "count": c}
                   for m, c in ranked if m != config.SYNTHETIC_MODEL],
        "estimated": bool(row["estimated"]),
        "cost": row["cost_usd"],
        "inTok": row["in_tok"],
        "outTok": row["out_tok"],
        "cacheWTok": row["cache_w_tok"],
        "cacheRTok": row["cache_r_tok"],
        "fileBytes": row["file_bytes"],
        "extraBytes": row["extra_bytes"],
        "totalBytes": row["file_bytes"] + row["extra_bytes"],
        "path": row["canonical_path"],
        "dupPaths": json.loads(row["dup_paths"] or "[]"),
    }
    if live_map is not None:
        info = live_map.get(row["session_id"])
        d["live"] = bool(info)
        d["liveStatus"] = info.get("status") if info else None
    return d


# ------------------------------------------------------------------ project labels
def _label_map(cwds) -> dict[str, str]:
    """cwd -> display label, disambiguating repeated leaf names ('rancho/backend').

    Shared by the sidebar and the charts so a project is never called two things in
    two places.
    """
    counts: dict[str, int] = {}
    for cwd in cwds:
        base = os.path.basename(cwd or "") or "—"
        counts[base] = counts.get(base, 0) + 1

    out: dict[str, str] = {}
    for cwd in cwds:
        base = os.path.basename(cwd or "") or "—"
        if counts.get(base, 0) > 1:
            parent = os.path.basename(os.path.dirname(cwd or ""))
            out[cwd] = f"{parent}/{base}" if parent else base
        else:
            out[cwd] = base
    return out


# -------------------------------------------------------------------------- overview
OVERVIEW_DAYS = 60
TOP_PROJECTS = 10


def overview(days: int = OVERVIEW_DAYS) -> dict:
    conn = db.connect()

    totals = conn.execute("""
        SELECT COUNT(*) n, SUM(cost_usd) cost, SUM(in_tok) i, SUM(out_tok) o,
               SUM(cache_w_tok) cw, SUM(cache_r_tok) cr,
               SUM(file_bytes) fb, SUM(extra_bytes) eb,
               SUM(estimated) est
        FROM sessions""").fetchone()

    # Sessions with almost no human input are the obvious cleanup candidates.
    junk = conn.execute("""
        SELECT COUNT(*) n, SUM(file_bytes + extra_bytes) bytes
        FROM sessions WHERE human_msgs < 3""").fetchone()

    # --- daily spend, stacked by model
    rows = conn.execute("""
        SELECT day, model, SUM(cost_usd) cost FROM usage_daily
        WHERE day >= date('now', ?) AND day != 'unknown'
        GROUP BY day, model ORDER BY day
    """, (f"-{days} day",)).fetchall()
    per_day: dict[str, dict[str, float]] = {}
    for r in rows:
        per_day.setdefault(r["day"], {})[r["model"]] = r["cost"]

    # --- model ordering is GLOBAL and stable: colour follows the model, never its
    # rank on the current chart, so filtering never repaints the survivors.
    model_totals = conn.execute("""
        SELECT model, SUM(cost_usd) cost FROM usage_daily
        GROUP BY model ORDER BY cost DESC""").fetchall()
    models = [{"id": r["model"], "name": config.display_name(r["model"]),
               "cost": r["cost"], "slot": config.model_slot(r["model"])}
              for r in model_totals]

    daily = [{"day": day, "byModel": per_day.get(day, {}),
              "total": sum(per_day.get(day, {}).values())}
             for day in sorted(per_day)]

    # --- spend per project (top N + Other)
    proj_rows = conn.execute("""
        SELECT cwd, SUM(cost_usd) cost, COUNT(*) n FROM sessions
        GROUP BY cwd ORDER BY cost DESC""").fetchall()
    labels = _label_map([r["cwd"] for r in proj_rows])
    top = [{"name": labels.get(r["cwd"], "—"), "cwd": r["cwd"],
            "cost": r["cost"] or 0, "count": r["n"]}
           for r in proj_rows[:TOP_PROJECTS]]
    rest = proj_rows[TOP_PROJECTS:]
    if rest:
        top.append({"name": f"Other ({len(rest)})", "cwd": None,
                    "cost": sum(r["cost"] or 0 for r in rest),
                    "count": sum(r["n"] for r in rest)})

    # --- cost split by token type, priced per model then summed
    by_type = {"input": 0.0, "output": 0.0, "cacheWrite": 0.0, "cacheRead": 0.0}
    savings = 0.0
    for r in conn.execute("""
        SELECT model, SUM(in_tok) i, SUM(out_tok) o, SUM(cache_w5_tok) w5,
               SUM(cache_w1h_tok) w1, SUM(cache_r_tok) cr
        FROM usage_daily GROUP BY model
    """):
        p_in, p_out, _ = config.price_for(r["model"])
        by_type["input"] += (r["i"] or 0) * p_in / 1e6
        by_type["output"] += (r["o"] or 0) * p_out / 1e6
        by_type["cacheWrite"] += ((r["w5"] or 0) * p_in * config.CACHE_WRITE_5M_MULT
                                  + (r["w1"] or 0) * p_in * config.CACHE_WRITE_1H_MULT) / 1e6
        by_type["cacheRead"] += (r["cr"] or 0) * p_in * config.CACHE_READ_MULT / 1e6
        # What those cache reads would have cost at full input price.
        savings += (r["cr"] or 0) * p_in * (1 - config.CACHE_READ_MULT) / 1e6

    return {
        "totals": {
            "sessions": totals["n"] or 0,
            "cost": totals["cost"] or 0,
            "inTok": totals["i"] or 0,
            "outTok": totals["o"] or 0,
            "cacheWTok": totals["cw"] or 0,
            "cacheRTok": totals["cr"] or 0,
            "bytes": (totals["fb"] or 0) + (totals["eb"] or 0),
            "estimated": bool(totals["est"]),
        },
        "reclaimable": {"count": junk["n"] or 0, "bytes": junk["bytes"] or 0},
        "projectCount": len(proj_rows),
        "models": models,
        "daily": daily,
        "days": days,
        "byProject": top,
        "costByType": by_type,
        "cacheSavings": savings,
        "costCaption": config.COST_CAPTION,
    }


# ------------------------------------------------------------------------------ plan
def plan(window_days: int = 7) -> dict:
    """Real plan utilization + which sessions/models are driving it.

    The utilization percentages are Anthropic's own (via plan_mod). The per-session
    attribution is a share of the user's OWN measured token usage in the window — the
    only honest per-session number, since Anthropic doesn't publish the window's size.
    """
    conn = db.connect()
    status = plan_mod.plan_status()

    # A "billable weight" that tracks what actually consumes a plan window better than a
    # raw token sum: output and cache-write dominate; cache-read is a tenth. Uses the
    # same list-price ratios the cost math uses, so the ranking matches spend intuition.
    def weight(p=""):
        return (f"({p}in_tok + {p}out_tok*5 + {p}cache_w5_tok*1.25 + "
                f"{p}cache_w1h_tok*2 + {p}cache_r_tok*0.1)")

    per_model = conn.execute(f"""
        SELECT model, SUM({weight()}) w, SUM(in_tok+out_tok+cache_w5_tok+cache_w1h_tok
               +cache_r_tok) tok, SUM(cost_usd) cost
        FROM usage_daily
        WHERE day >= date('now', ?) AND day != 'unknown'
        GROUP BY model ORDER BY w DESC
    """, (f"-{window_days} day",)).fetchall()
    total_w = sum(r["w"] for r in per_model) or 1
    models = [{"id": r["model"], "name": config.display_name(r["model"]),
               "slot": config.model_slot(r["model"]),
               "share": r["w"] / total_w, "tokens": r["tok"], "cost": r["cost"]}
              for r in per_model if r["model"] != config.SYNTHETIC_MODEL]

    rows = conn.execute(f"""
        SELECT ud.session_id, SUM({weight('ud.')}) w,
               SUM(ud.in_tok+ud.out_tok+ud.cache_w5_tok+ud.cache_w1h_tok+ud.cache_r_tok) tok,
               SUM(ud.cost_usd) cost, s.title, s.cwd
        FROM usage_daily ud JOIN sessions s ON s.session_id = ud.session_id
        WHERE ud.day >= date('now', ?) AND ud.day != 'unknown'
        GROUP BY ud.session_id ORDER BY w DESC LIMIT 12
    """, (f"-{window_days} day",)).fetchall()
    live_map = live.live_sessions()
    sessions = [{
        "id": r["session_id"], "title": r["title"],
        "project": os.path.basename(r["cwd"] or "") or "—",
        "share": r["w"] / total_w, "tokens": r["tok"], "cost": r["cost"],
        "live": r["session_id"] in live_map,
    } for r in rows]

    return {
        "status": status,
        "windowDays": window_days,
        "models": models,
        "sessions": sessions,
        "daily": plan_mod.daily_activity()[-window_days * 3:],
        "costCaption": config.COST_CAPTION,
    }


# --------------------------------------------------------------------------- sidebar
def projects() -> list[dict]:
    """One entry per distinct cwd, most recently active first."""
    conn = db.connect()
    live_map = live.live_sessions()
    live_cwds = {i.get("cwd") for i in live_map.values()}

    rows = conn.execute("""
        SELECT cwd, COUNT(*) n, MAX(last_ts) last_ts, SUM(cost_usd) cost,
               SUM(file_bytes + extra_bytes) bytes
        FROM sessions GROUP BY cwd ORDER BY last_ts DESC
    """).fetchall()

    labels = _label_map([r["cwd"] for r in rows])

    out = []
    for r in rows:
        cwd = r["cwd"] or ""
        out.append({
            "name": labels.get(r["cwd"], "—"), "cwd": cwd, "count": r["n"],
            "cost": r["cost"] or 0,
            "bytes": r["bytes"] or 0, "lastTs": r["last_ts"],
            "live": cwd in live_cwds, "exists": bool(cwd) and os.path.isdir(cwd),
        })
    return out


# ------------------------------------------------------------------------- sessions
_SORTS = {
    "recent": "last_ts DESC",
    "cost": "cost_usd DESC",
    "size": "(file_bytes + extra_bytes) DESC",
    "messages": "human_msgs DESC",
    "title": "title COLLATE NOCASE ASC",
}


def sessions(cwd: str | None = None, sort: str = "recent") -> dict:
    conn = db.connect()
    live_map = live.live_sessions()
    order = _SORTS.get(sort, _SORTS["recent"])
    if cwd:
        rows = conn.execute(
            f"SELECT {SESSION_COLS} FROM sessions WHERE cwd=? ORDER BY {order}",
            (cwd,)).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {SESSION_COLS} FROM sessions ORDER BY {order}").fetchall()
    items = [_session_dict(r, live_map) for r in rows]
    return {
        "cwd": cwd,
        "name": os.path.basename(cwd) if cwd else "All sessions",
        "exists": bool(cwd) and os.path.isdir(cwd),
        "sort": sort,
        "sessions": items,
        "totals": {
            "count": len(items),
            "cost": sum(i["cost"] for i in items),
            "bytes": sum(i["totalBytes"] for i in items),
        },
    }


def session(session_id: str) -> dict:
    conn = db.connect()
    row = conn.execute(
        f"SELECT {SESSION_COLS} FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown session {session_id}")
    d = _session_dict(row, live.live_sessions())

    # Per-model cost breakdown for the detail card.
    d["byModel"] = [
        {"id": r["model"], "name": config.display_name(r["model"]),
         "cost": r["cost"], "inTok": r["i"], "outTok": r["o"],
         "cacheWTok": r["w"], "cacheRTok": r["r"]}
        for r in conn.execute("""
            SELECT model, SUM(cost_usd) cost, SUM(in_tok) i, SUM(out_tok) o,
                   SUM(cache_w5_tok + cache_w1h_tok) w, SUM(cache_r_tok) r
            FROM usage_daily WHERE session_id=? GROUP BY model ORDER BY cost DESC
        """, (session_id,))
    ]
    d["messageCount"] = conn.execute(
        "SELECT COUNT(*) c FROM message_index WHERE session_id=?",
        (session_id,)).fetchone()["c"]
    # The remainder are user-role records carrying only tool_result blocks — real rows in
    # the transcript, but neither prompts nor replies. Surfacing them keeps the card's
    # numbers adding up to messageCount.
    d["toolMsgs"] = max(0, d["messageCount"] - d["humanMsgs"] - d["assistantMsgs"])
    d["cwdExists"] = bool(d["cwd"]) and os.path.isdir(d["cwd"])
    d["hasPlan"] = bool(row["slug"]) and (config.PLANS_DIR / f"{row['slug']}.md").exists()
    return d


# --------------------------------------------------------------------------- cleanup
THROWAWAY_HUMAN_MSGS = 3


def cleanup_list(sort: str = "size") -> dict:
    """Every session with what it costs on disk, flagged for triage.

    Live sessions are returned but marked locked — the UI must never let them be
    trashed, and actions.trash_sessions re-checks liveness at action time anyway.
    """
    conn = db.connect()
    live_map = live.live_sessions()
    order = {"size": "(file_bytes + extra_bytes) DESC",
             "age": "last_ts ASC",
             "messages": "human_msgs ASC",
             "cost": "cost_usd DESC"}.get(sort, "(file_bytes + extra_bytes) DESC")

    rows = conn.execute(f"SELECT {SESSION_COLS} FROM sessions ORDER BY {order}").fetchall()
    items = []
    for r in rows:
        d = _session_dict(r, live_map)
        d["throwaway"] = r["human_msgs"] < THROWAWAY_HUMAN_MSGS
        d["locked"] = d["live"]
        items.append(d)

    return {
        "sort": sort,
        "sessions": items,
        "totals": {
            "count": len(items),
            "bytes": sum(i["totalBytes"] for i in items),
            "throwawayCount": sum(1 for i in items if i["throwaway"] and not i["locked"]),
            "throwawayBytes": sum(i["totalBytes"] for i in items
                                  if i["throwaway"] and not i["locked"]),
            "lockedCount": sum(1 for i in items if i["locked"]),
        },
    }


def session_paths(session_id: str) -> list[str]:
    """Every path on disk that belongs to this session and nothing else.

    Deliberately excludes ~/.claude/file-history, which is shared across sessions.
    """
    conn = db.connect()
    row = conn.execute(
        "SELECT canonical_path, dup_paths FROM sessions WHERE session_id=?",
        (session_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown session {session_id}")

    paths: list[Path] = []
    jsonl = [Path(row["canonical_path"])] + [Path(p) for p in
                                             json.loads(row["dup_paths"] or "[]")]
    for p in jsonl:
        paths.append(p)                       # <uuid>.jsonl
        paths.append(p.parent / session_id)   # <uuid>/  (subagents, workflows)
        # sibling metadata written next to the transcript, e.g. <uuid>.meta.json
        try:
            paths.extend(sib for sib in p.parent.glob(f"{session_id}.*") if sib != p)
        except OSError:
            pass

    paths.append(config.SESSION_ENV_DIR / session_id)
    paths.append(config.TASKS_DIR / session_id)
    paths.append(config.JOBS_DIR / session_id[:8])

    seen, out = set(), []
    for p in paths:
        s = str(p)
        if s not in seen and p.exists():
            seen.add(s)
            out.append(s)
    return out


# ---------------------------------------------------------------------------- search
import re as _re
import sqlite3 as _sqlite3

_FTS_TOKEN = _re.compile(r'"[^"]*"|\S+')


def _quote_query(q: str) -> str:
    """Fallback: quote every token so FTS5 treats them as literals.

    Raw input is tried first (so `OR`, `NEAR`, prefix* keep working), but anything
    with unbalanced quotes or stray operators — `c++`, `foo AND`, `a:b` — is a syntax
    error in FTS5. Quoting each token makes any input searchable as plain words.
    """
    toks = [t.strip('"') for t in _FTS_TOKEN.findall(q)]
    toks = [t.replace('"', '') for t in toks if t.strip()]
    return " ".join(f'"{t}"' for t in toks)


def search(q: str, cwd: str | None = None, limit: int = 300) -> dict:
    q = (q or "").strip()
    if not q:
        return {"query": q, "hits": [], "sessions": [], "total": 0}

    conn = db.connect()
    params: list = []
    where = "messages_fts MATCH ?"
    if cwd:
        where += " AND f.session_id IN (SELECT session_id FROM sessions WHERE cwd=?)"

    sql = f"""
        SELECT f.session_id, f.role, f.msg_idx, f.ts,
               snippet(messages_fts, 0, '\x02', '\x03', '…', 14) AS snip,
               s.title, s.cwd
        FROM messages_fts f JOIN sessions s ON s.session_id = f.session_id
        WHERE {where}
        ORDER BY rank LIMIT ?
    """

    def run(match: str):
        p = [match] + ([cwd] if cwd else []) + [limit]
        return conn.execute(sql, p).fetchall()

    try:
        rows = run(q)
    except _sqlite3.OperationalError:
        try:
            # Quoted form fixes operator/quote noise, but a query that reduces to
            # nothing (just `"` characters) yields MATCH '' — a syntax error again.
            # An unsearchable input returns zero hits, same as a search with no match.
            rows = run(_quote_query(q)) if _quote_query(q).strip() else []
        except _sqlite3.OperationalError:
            rows = []

    by_session: dict[str, dict] = {}
    for r in rows:
        g = by_session.setdefault(r["session_id"], {
            "id": r["session_id"], "title": r["title"], "cwd": r["cwd"],
            "project": os.path.basename(r["cwd"] or "") or "—", "hits": [],
        })
        g["hits"].append({
            "msgIdx": r["msg_idx"], "role": r["role"], "ts": r["ts"],
            "snippet": r["snip"],
        })
    groups = sorted(by_session.values(), key=lambda g: -len(g["hits"]))
    return {"query": q, "sessions": groups, "total": len(rows),
            "truncated": len(rows) >= limit}


# ------------------------------------------------------------------------ transcript
TOOL_RESULT_CAP = 2000
TEXT_CAP = 20000


def _clip(s: str, cap: int) -> tuple[str, bool]:
    if s is None:
        return "", False
    return (s[:cap], True) if len(s) > cap else (s, False)


def _tool_result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = []
        for b in c:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text") or "")
                else:
                    parts.append(f"[{b.get('type')}]")
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return "" if c is None else str(c)


def _blocks(rec: dict) -> list[dict]:
    msg = rec.get("message") or {}
    content = msg.get("content")
    if isinstance(content, str):
        text, clipped = _clip(content, TEXT_CAP)
        return [{"type": "text", "text": text, "clipped": clipped}]
    if not isinstance(content, list):
        return []

    out = []
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            text, clipped = _clip(b.get("text") or "", TEXT_CAP)
            out.append({"type": "text", "text": text, "clipped": clipped})
        elif t == "thinking":
            text, clipped = _clip(b.get("thinking") or "", TEXT_CAP)
            out.append({"type": "thinking", "text": text, "clipped": clipped})
        elif t == "tool_use":
            raw = json.dumps(b.get("input") or {}, ensure_ascii=False)
            text, clipped = _clip(raw, TOOL_RESULT_CAP)
            out.append({"type": "tool_use", "name": b.get("name") or "tool",
                        "input": text, "clipped": clipped})
        elif t == "tool_result":
            text, clipped = _clip(_tool_result_text(b), TOOL_RESULT_CAP)
            out.append({"type": "tool_result", "text": text, "clipped": clipped,
                        "isError": bool(b.get("is_error"))})
        elif t == "image":
            out.append({"type": "image"})
        else:
            out.append({"type": t or "unknown"})
    return out


def transcript(session_id: str, start: int | None = None, limit: int = 100) -> dict:
    """A page of messages, read by seeking into the canonical .jsonl.

    Transcript text is never stored in the index — message_index holds byte offsets, so
    even a 93MB / 10k-message session pages in a few milliseconds.
    """
    conn = db.connect()
    srow = conn.execute(
        "SELECT canonical_path FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if srow is None:
        raise KeyError(f"unknown session {session_id}")

    total = conn.execute("SELECT COUNT(*) c FROM message_index WHERE session_id=?",
                         (session_id,)).fetchone()["c"]
    if start is None:                      # default to the newest page
        start = max(0, total - limit)
    start = max(0, min(start, max(0, total - 1)))

    rows = conn.execute("""
        SELECT msg_idx, byte_off, byte_len, role, ts FROM message_index
        WHERE session_id=? AND msg_idx >= ? ORDER BY msg_idx LIMIT ?
    """, (session_id, start, limit)).fetchall()

    items: list[dict] = []
    path = srow["canonical_path"]
    try:
        with open(path, "rb") as f:
            for r in rows:
                f.seek(r["byte_off"])
                raw = f.read(r["byte_len"])
                try:
                    rec = json.loads(raw.strip().decode("utf-8", "replace"))
                except Exception:
                    items.append({"idx": r["msg_idx"], "role": r["role"], "ts": r["ts"],
                                  "blocks": [], "error": "unreadable record"})
                    continue
                msg = rec.get("message") or {}
                items.append({
                    "idx": r["msg_idx"],
                    "role": r["role"],
                    "ts": r["ts"],
                    "model": msg.get("model"),
                    "isCompactSummary": bool(rec.get("isCompactSummary")),
                    "blocks": _blocks(rec),
                })
    except FileNotFoundError:
        raise FileNotFoundError(f"session file is gone: {path}")

    return {
        "sessionId": session_id, "total": total, "start": start,
        "count": len(items),
        "hasBefore": start > 0,
        "hasAfter": start + len(items) < total,
        "messages": items,
    }
