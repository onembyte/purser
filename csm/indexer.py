"""Scan ~/.claude, dedupe sessions, and keep the SQLite index fresh.

Pipeline
--------
1. Glob `projects/*/<uuid>.jsonl` at depth 1 only. Anything under `<uuid>/` is child data.
2. Group by sessionId **across project dirs** — a session that changed cwd exists twice.
   Canonical = largest, then newest. The others are recorded as dups (their bytes still
   count toward disk usage).
3. Collect `<uuid>/subagents/*.jsonl` for cost attribution.
4. Diff (path, mtime, size) against the `files` table -> dirty set.
5. Parse each dirty session and replace its rows in one transaction.

Run standalone to verify without the GUI:  python -m csm.indexer
"""
from __future__ import annotations

import json
import os
import queue
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from csm import config, db
from csm.parser import SessionParse, Usage, parse_session, parse_usage_only

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class FileRef:
    path: str
    mtime: float
    size: int


@dataclass
class SessionFiles:
    session_id: str
    canonical: FileRef
    project_dir: str
    dups: list[FileRef] = field(default_factory=list)
    subagents: list[FileRef] = field(default_factory=list)

    def signature(self) -> list[tuple[str, float, int]]:
        refs = [self.canonical, *self.dups, *self.subagents]
        return sorted((r.path, r.mtime, r.size) for r in refs)


def _ref(p: Path) -> FileRef | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return FileRef(str(p), st.st_mtime, st.st_size)


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(p, onerror=lambda e: None):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


# --------------------------------------------------------------------------- scan
def scan() -> dict[str, SessionFiles]:
    """Filesystem -> {session_id: SessionFiles}, deduped across project dirs."""
    candidates: dict[str, list[tuple[FileRef, str]]] = {}

    if not config.PROJECTS_DIR.is_dir():
        return {}

    for project in config.PROJECTS_DIR.iterdir():
        if not project.is_dir():
            continue
        try:
            entries = list(project.iterdir())
        except OSError:
            continue
        for f in entries:
            # Depth 1, .jsonl, uuid-named. Excludes memory/*.md, *.meta.json,
            # <uuid>/workflows/*.json and <uuid>/subagents/journal.jsonl.
            if f.suffix != ".jsonl" or not UUID_RE.match(f.stem) or not f.is_file():
                continue
            ref = _ref(f)
            if ref:
                candidates.setdefault(f.stem, []).append((ref, str(project)))

    sessions: dict[str, SessionFiles] = {}
    for sid, refs in candidates.items():
        # Canonical = biggest (the grown continuation), tie-break newest.
        refs.sort(key=lambda t: (t[0].size, t[0].mtime), reverse=True)
        canonical, project_dir = refs[0]
        sf = SessionFiles(session_id=sid, canonical=canonical, project_dir=project_dir,
                          dups=[r for r, _ in refs[1:]])
        # Subagent transcripts nest arbitrarily deep: both `<uuid>/subagents/agent-*.jsonl`
        # and `<uuid>/subagents/workflows/wf_<id>/agent-*.jsonl` occur, so walk the whole
        # subtree. The `agent-*` prefix excludes workflow `journal.jsonl` files, which are
        # job records rather than transcripts and carry no usage.
        seen: set[str] = set()
        for _r, proj in refs:
            sub_dir = Path(proj) / sid / "subagents"
            if not sub_dir.is_dir():
                continue
            try:
                for s in sub_dir.rglob("agent-*.jsonl"):
                    if not s.is_file() or str(s) in seen:
                        continue
                    r = _ref(s)
                    if r:
                        seen.add(str(s))
                        sf.subagents.append(r)
            except OSError:
                pass
        sessions[sid] = sf
    return sessions


def _extra_bytes(sf: SessionFiles) -> int:
    """Ancillary per-session data that cleanup would also reclaim."""
    sid = sf.session_id
    total = 0
    for _ref_ in (sf.canonical, *sf.dups):
        total += _dir_size(Path(_ref_.path).parent / sid)
    total += _dir_size(config.SESSION_ENV_DIR / sid)
    total += _dir_size(config.TASKS_DIR / sid)
    total += _dir_size(config.JOBS_DIR / sid[:8])
    return total


# --------------------------------------------------------------------------- diff
def dirty_sessions(found: dict[str, SessionFiles]) -> tuple[list[str], list[str]]:
    """-> (session_ids needing reindex, session_ids to drop)."""
    conn = db.connect()
    known: dict[str, list[tuple[str, float, int]]] = {}
    for row in conn.execute("SELECT path, session_id, mtime, size FROM files"):
        known.setdefault(row["session_id"], []).append(
            (row["path"], row["mtime"], row["size"]))

    dirty = [sid for sid, sf in found.items()
             if sorted(known.get(sid, [])) != sf.signature()]
    gone = [sid for sid in known if sid not in found]
    return dirty, gone


# -------------------------------------------------------------------------- write
def index_session(sf: SessionFiles) -> SessionParse | None:
    parsed = parse_session(Path(sf.canonical.path), sf.session_id)

    usage = Usage()
    usage.merge(parsed.usage)
    for sub in sf.subagents:
        usage.merge(parse_usage_only(Path(sub.path)))

    file_bytes = sf.canonical.size + sum(d.size for d in sf.dups)
    extra = _extra_bytes(sf)

    conn = db.connect()
    with conn:                                   # one transaction per session
        conn.execute("DELETE FROM messages_fts WHERE session_id=?", (sf.session_id,))
        conn.execute("DELETE FROM message_index WHERE session_id=?", (sf.session_id,))
        conn.execute("DELETE FROM usage_daily WHERE session_id=?", (sf.session_id,))
        conn.execute("DELETE FROM files WHERE session_id=?", (sf.session_id,))

        conn.execute("""
            INSERT OR REPLACE INTO sessions(
              session_id, canonical_path, project_dir, cwd, git_branch, title,
              title_source, first_ts, last_ts, cli_version, human_msgs, assistant_msgs,
              total_records, malformed_lines, models, estimated, cost_usd, in_tok,
              out_tok, cache_w_tok, cache_r_tok, file_bytes, extra_bytes, dup_paths, slug)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            sf.session_id, sf.canonical.path, sf.project_dir, parsed.cwd,
            parsed.git_branch, parsed.title, parsed.title_source, parsed.first_ts,
            parsed.last_ts, parsed.cli_version, parsed.human_msgs, parsed.assistant_msgs,
            parsed.total_records, parsed.malformed_lines, json.dumps(dict(usage.models)),
            int(usage.estimated), usage.cost_usd, usage.in_tok, usage.out_tok,
            usage.cache_w_tok, usage.cache_r_tok, file_bytes, extra,
            json.dumps([d.path for d in sf.dups]), parsed.slug,
        ))

        conn.executemany(
            "INSERT INTO message_index(session_id, msg_idx, byte_off, byte_len, role, ts)"
            " VALUES(?,?,?,?,?,?)",
            [(sf.session_id, m.msg_idx, m.byte_off, m.byte_len, m.role, m.ts)
             for m in parsed.messages])

        fts_rows = [(m.fts_text, sf.session_id, m.role, m.ts, m.msg_idx)
                    for m in parsed.messages if m.fts_text]
        if parsed.title and parsed.title_source in ("custom", "ai"):
            fts_rows.append((parsed.title, sf.session_id, "title", parsed.last_ts, -1))
        conn.executemany(
            "INSERT INTO messages_fts(text, session_id, role, ts, msg_idx)"
            " VALUES(?,?,?,?,?)", fts_rows)

        conn.executemany("""
            INSERT INTO usage_daily(day, session_id, model, in_tok, out_tok,
                                    cache_w5_tok, cache_w1h_tok, cache_r_tok, cost_usd)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, [(day, sf.session_id, model, d["in_tok"], d["out_tok"], d["cache_w5_tok"],
               d["cache_w1h_tok"], d["cache_r_tok"], d["cost_usd"])
              for (day, model), d in usage.daily.items()])

        rows = [(sf.canonical.path, sf.session_id, "main", sf.canonical.mtime,
                 sf.canonical.size, 1, None)]
        rows += [(d.path, sf.session_id, "dup", d.mtime, d.size, 1, None) for d in sf.dups]
        rows += [(s.path, sf.session_id, "subagent", s.mtime, s.size, 1, None)
                 for s in sf.subagents]
        conn.executemany(
            "INSERT OR REPLACE INTO files(path, session_id, kind, mtime, size,"
            " parsed_ok, error) VALUES(?,?,?,?,?,?,?)", rows)

    parsed.usage = usage
    return parsed


def drop_session(session_id: str) -> None:
    conn = db.connect()
    with conn:
        for table in ("messages_fts", "message_index", "usage_daily", "files", "sessions"):
            conn.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))


# ------------------------------------------------------------------------- driver
def reindex(progress=None) -> dict:
    """Full incremental pass. `progress(done, total, title)` is called on this thread."""
    t0 = time.time()
    found = scan()
    dirty, gone = dirty_sessions(found)

    for sid in gone:
        drop_session(sid)

    # Newest first: the sessions the user is most likely to want appear soonest.
    dirty.sort(key=lambda s: found[s].canonical.mtime, reverse=True)
    errors = 0
    for i, sid in enumerate(dirty):
        try:
            parsed = index_session(found[sid])
            if progress:
                progress(i + 1, len(dirty), parsed.title if parsed else sid)
        except (OSError, ValueError, sqlite3.Error) as exc:
            errors += 1
            print(f"  ! {sid}: {type(exc).__name__}: {exc}")

    db.set_meta("last_scan_ts", time.time())
    return {"total": len(found), "indexed": len(dirty), "dropped": len(gone),
            "errors": errors, "seconds": round(time.time() - t0, 2)}


class IndexerThread(threading.Thread):
    """Background reindex driver. `on_progress`/`on_done` fire on this thread."""

    def __init__(self, on_progress=None, on_done=None):
        super().__init__(name="csm-indexer", daemon=True)
        self._wake: queue.Queue = queue.Queue()
        self._on_progress = on_progress
        self._on_done = on_done

    def request(self) -> None:
        self._wake.put(True)

    def run(self) -> None:
        db.init()
        while True:
            self._wake.get()
            # Coalesce bursts of requests into one pass.
            while not self._wake.empty():
                self._wake.get_nowait()
            try:
                stats = reindex(self._on_progress)
                if self._on_done:
                    self._on_done(stats)
            except Exception as exc:  # never let the thread die
                print(f"indexer error: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------- CLI harness
def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _main() -> int:
    import sys
    reset = "--reset" in sys.argv
    print(f"claude root : {config.CLAUDE_ROOT}")
    print(f"index db    : {config.DB_PATH}")
    db.init(reset=reset)

    def prog(done, total, title):
        print(f"  [{done:>3}/{total}] {(title or '')[:64]}")

    stats = reindex(prog)
    print(f"\nscan: {stats['total']} sessions, indexed {stats['indexed']}, "
          f"dropped {stats['dropped']}, errors {stats['errors']}, "
          f"{stats['seconds']}s")

    conn = db.connect()
    row = conn.execute("""
        SELECT COUNT(*) n, SUM(cost_usd) cost, SUM(in_tok) i, SUM(out_tok) o,
               SUM(cache_w_tok) cw, SUM(cache_r_tok) cr,
               SUM(file_bytes) fb, SUM(extra_bytes) eb, SUM(malformed_lines) bad
        FROM sessions""").fetchone()
    print(f"\nsessions    : {row['n']}")
    print(f"cost        : ${row['cost'] or 0:,.2f}   ({config.COST_CAPTION})")
    print(f"tokens      : in {row['i']:,}  out {row['o']:,}  "
          f"cache-w {row['cw']:,}  cache-r {row['cr']:,}")
    print(f"disk        : jsonl {_human(row['fb'] or 0)}  "
          f"+ ancillary {_human(row['eb'] or 0)}")
    print(f"malformed   : {row['bad']} lines")

    fts = conn.execute("SELECT COUNT(*) n FROM messages_fts").fetchone()["n"]
    mi = conn.execute("SELECT COUNT(*) n FROM message_index").fetchone()["n"]
    print(f"indexed     : {mi:,} messages, {fts:,} searchable")

    print("\nrecords per model (cross-check against the corpus):")
    counts: dict[str, int] = {}
    for r in conn.execute("SELECT models FROM sessions"):
        for m, c in json.loads(r["models"] or "{}").items():
            counts[m] = counts.get(m, 0) + c
    for m, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {m:<32} {c:>7,}")

    print("\ntop sessions by cost:")
    for r in conn.execute("""SELECT title, cost_usd, human_msgs, file_bytes, cwd
                             FROM sessions ORDER BY cost_usd DESC LIMIT 12"""):
        proj = Path(r["cwd"] or "?").name
        print(f"  ${r['cost_usd']:>8.2f}  {r['human_msgs']:>4} msgs  "
              f"{_human(r['file_bytes']):>8}  {proj:<20} {(r['title'] or '')[:44]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
