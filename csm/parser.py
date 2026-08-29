"""Streaming parser for a single Claude Code session .jsonl file.

Correctness notes (all verified against the real corpus):

* Files are read in **binary** so recorded byte offsets are exact; each line is decoded
  with errors='replace'. A malformed line is counted and skipped rather than fatal —
  which also self-heals the torn final line that appears while Claude Code is actively
  appending to a session we are indexing.
* Title precedence: last `custom-title` > last `ai-title` > first human prompt >
  `last-prompt`. The title records repeat throughout the file, so only the LAST wins.
* Cost includes subagent transcripts (`<uuid>/subagents/*.jsonl`), which carry their own
  usage on every assistant record and are billed to the parent session.
* `<synthetic>` records carry all-zero usage and are excluded from cost.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from csm import config

FTS_TEXT_CAP = 8192          # per message; keeps the index small
TITLE_CAP = 90

# Harness-injected wrappers and slash commands are noise for search and titles.
_NOISE_PREFIXES = (
    "<local-command", "<command-name", "<command-message", "<command-args",
    "<bash-input", "<bash-stdout", "<bash-stderr", "<system-reminder",
    "Caveat: The messages below",
)


def _is_noise(text: str) -> bool:
    t = text.lstrip()
    if not t:
        return True
    if t.startswith(_NOISE_PREFIXES):
        return True
    return False


def _day_local(ts: str | None) -> str | None:
    """ISO-8601 UTC timestamp -> local calendar day (charts should match the user's days)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d")
    except Exception:
        return ts[:10] or None


def _user_text(message: dict) -> str | None:
    """Human-authored text, or None for tool_result-only / empty records."""
    content = message.get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        parts = [b.get("text") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"
                 and isinstance(b.get("text"), str)]
        return "\n".join(parts) if parts else None
    return None


def _assistant_text(message: dict) -> str | None:
    content = message.get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        # text blocks only: tool_use inputs and thinking are not what people search for
        parts = [b.get("text") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"
                 and isinstance(b.get("text"), str)]
        return "\n".join(parts) if parts else None
    return None


def _split_usage(usage: dict) -> tuple[int, int, int, int, int]:
    """-> (in, out, cache_write_5m, cache_write_1h, cache_read)."""
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    cache_r = int(usage.get("cache_read_input_tokens") or 0)

    cc = usage.get("cache_creation")
    if isinstance(cc, dict) and (
        cc.get("ephemeral_5m_input_tokens") is not None
        or cc.get("ephemeral_1h_input_tokens") is not None
    ):
        cw5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
        cw1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    else:
        # Older/other shape: price the flat total at the 5-minute rate.
        cw5 = int(usage.get("cache_creation_input_tokens") or 0)
        cw1h = 0
    return in_tok, out_tok, cw5, cw1h, cache_r


@dataclass
class MessageRow:
    msg_idx: int
    byte_off: int
    byte_len: int
    role: str
    ts: str | None
    fts_text: str | None


@dataclass
class Usage:
    """Token/cost accumulator, keyed by (day, model) for chart rollups."""
    models: Counter = field(default_factory=Counter)
    daily: dict = field(default_factory=dict)   # (day, model) -> dict
    in_tok: int = 0
    out_tok: int = 0
    cache_w_tok: int = 0
    cache_r_tok: int = 0
    cost_usd: float = 0.0
    estimated: bool = False

    def add(self, model: str, ts: str | None, usage: dict) -> None:
        self.models[model] += 1
        if model == config.SYNTHETIC_MODEL:
            return
        in_tok, out_tok, cw5, cw1h, cr = _split_usage(usage)
        if not (in_tok or out_tok or cw5 or cw1h or cr):
            return
        cost = config.cost_of(model, in_tok, out_tok, cw5, cw1h, cr)
        if config.price_for(model)[2]:
            self.estimated = True

        self.in_tok += in_tok
        self.out_tok += out_tok
        self.cache_w_tok += cw5 + cw1h
        self.cache_r_tok += cr
        self.cost_usd += cost

        day = _day_local(ts) or "unknown"
        key = (day, model)
        d = self.daily.get(key)
        if d is None:
            d = self.daily[key] = {"in_tok": 0, "out_tok": 0, "cache_w5_tok": 0,
                                   "cache_w1h_tok": 0, "cache_r_tok": 0, "cost_usd": 0.0}
        d["in_tok"] += in_tok
        d["out_tok"] += out_tok
        d["cache_w5_tok"] += cw5
        d["cache_w1h_tok"] += cw1h
        d["cache_r_tok"] += cr
        d["cost_usd"] += cost

    def merge(self, other: "Usage") -> None:
        self.models.update(other.models)
        self.in_tok += other.in_tok
        self.out_tok += other.out_tok
        self.cache_w_tok += other.cache_w_tok
        self.cache_r_tok += other.cache_r_tok
        self.cost_usd += other.cost_usd
        self.estimated = self.estimated or other.estimated
        for key, d in other.daily.items():
            cur = self.daily.get(key)
            if cur is None:
                self.daily[key] = dict(d)
            else:
                for k, v in d.items():
                    cur[k] += v


@dataclass
class SessionParse:
    session_id: str
    path: str
    cwd: str | None = None
    git_branch: str | None = None
    cli_version: str | None = None
    slug: str | None = None
    title: str | None = None
    title_source: str = "none"
    first_ts: str | None = None
    last_ts: str | None = None
    human_msgs: int = 0
    assistant_msgs: int = 0
    total_records: int = 0
    malformed_lines: int = 0
    usage: Usage = field(default_factory=Usage)
    messages: list[MessageRow] = field(default_factory=list)


def parse_usage_only(path: Path) -> Usage:
    """Subagent transcripts: fold their spend into the parent, ignore their text."""
    usage = Usage()
    try:
        with open(path, "rb") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "assistant":
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                u = msg.get("usage")
                if isinstance(u, dict):
                    usage.add(msg.get("model") or "unknown", rec.get("timestamp"), u)
    except (OSError, ValueError):
        pass
    return usage


def parse_session(path: Path, session_id: str) -> SessionParse:
    out = SessionParse(session_id=session_id, path=str(path))

    custom_title = ai_title = last_prompt = first_human = None
    # A session's cwd is not stable: one real session here changes directory 298 times
    # across 7 dirs (repo root -> app/ -> api/ -> ...). "Which project is this?" means the
    # directory the work actually happened in, so cwd is the mode over records, not the
    # last value seen. Branch/version stay last-wins — those genuinely mean "current".
    cwd_counts: Counter = Counter()
    offset = 0
    msg_idx = 0

    with open(path, "rb") as f:
        for raw in f:
            start = offset
            offset += len(raw)
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                out.malformed_lines += 1
                continue
            if not isinstance(rec, dict):
                out.malformed_lines += 1
                continue

            out.total_records += 1
            rtype = rec.get("type")

            # -------------------------------------------------- title-ish records
            if rtype == "custom-title":
                custom_title = rec.get("customTitle") or custom_title
                continue
            if rtype == "ai-title":
                ai_title = rec.get("aiTitle") or ai_title
                continue
            if rtype == "last-prompt":
                last_prompt = rec.get("lastPrompt") or last_prompt
                continue
            if rtype not in ("user", "assistant"):
                continue

            # ------------------------------------------------------ conversation
            if rec.get("isSidechain"):          # defensive: never true in main files
                continue

            ts = rec.get("timestamp")
            if ts:
                if out.first_ts is None or ts < out.first_ts:
                    out.first_ts = ts
                if out.last_ts is None or ts > out.last_ts:
                    out.last_ts = ts
            cwd = rec.get("cwd")
            if cwd:
                cwd_counts[cwd] += 1
            for key, attr in (("gitBranch", "git_branch"),
                              ("version", "cli_version"), ("slug", "slug")):
                v = rec.get(key)
                if v:
                    setattr(out, attr, v)

            # `message` has no shape contract in the wild: a str or list here (seen in
            # real files) must skip the record's text, not AttributeError out of the
            # whole reindex pass.
            message = rec.get("message")
            if not isinstance(message, dict):
                message = {}
            fts_text = None

            if rtype == "user":
                # tool_result-only and compact-summary records are NOT human turns and
                # are never searchable, but they must still get a message_index row —
                # the transcript viewer renders them, and msg_idx must stay dense so
                # paging and search jump-to-message address the same sequence.
                text = None if rec.get("isCompactSummary") else _user_text(message)
                if text is not None:
                    out.human_msgs += 1
                    if not _is_noise(text):
                        fts_text = text[:FTS_TEXT_CAP]
                        if first_human is None:
                            first_human = text
            else:
                out.assistant_msgs += 1
                u = message.get("usage")
                if isinstance(u, dict):
                    out.usage.add(message.get("model") or "unknown", ts, u)
                text = _assistant_text(message)
                if text and not _is_noise(text):
                    fts_text = text[:FTS_TEXT_CAP]

            out.messages.append(MessageRow(msg_idx, start, len(raw), rtype, ts, fts_text))
            msg_idx += 1

    # Mode of cwd; ties resolve to the earliest-seen (Counter keeps insertion order).
    if cwd_counts:
        out.cwd = cwd_counts.most_common(1)[0][0]

    # ------------------------------------------------------------------ title
    if custom_title:
        out.title, out.title_source = custom_title, "custom"
    elif ai_title:
        out.title, out.title_source = ai_title, "ai"
    elif first_human:
        out.title, out.title_source = _summarize(first_human), "prompt"
    elif last_prompt:
        out.title, out.title_source = _summarize(last_prompt), "prompt"
    else:
        out.title, out.title_source = "Untitled session", "none"

    return out


_MD_HEADING = re.compile(r"^#{1,6}\s*")
_MD_EMPHASIS = re.compile(r"[*`_]{1,3}")


def _clip(s: str) -> str:
    return s[:TITLE_CAP] + ("…" if len(s) > TITLE_CAP else "")


def _summarize(text: str) -> str:
    """Title from a raw prompt.

    Prompts are often whole markdown documents; flattening the entire thing produces
    titles like '# Daily Curator — system prompt You are the **daily Curator** for…'.
    A leading heading is the author's own summary, so prefer it.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return "Untitled session"

    first = _MD_EMPHASIS.sub("", _MD_HEADING.sub("", lines[0]))
    first = " ".join(first.split())
    if len(first) >= 12:                       # a real first line, not a stray token
        return _clip(first)

    whole = _MD_EMPHASIS.sub("", " ".join(text.split()))
    return _clip(whole) if whole else "Untitled session"
