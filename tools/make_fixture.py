#!/usr/bin/env python3
"""Generate a synthetic ~/.claude corpus for screenshots and demos.

Purser renders your *real* sessions — real project names, prompts and dollar amounts.
Those must never end up in a public README, so the showcase screenshots are rendered
against this fabricated corpus instead. It writes the exact on-disk shape the parser
expects (see csm/parser.py): projects/<encoded-cwd>/<uuid>.jsonl with user/assistant
records carrying message.usage.

    python tools/make_fixture.py <output-root>          # e.g. /tmp/purser-fixture
    CSM_CLAUDE_ROOT=<root> CSM_APP_SUPPORT=<root>-support \
        python -m csm.indexer --reset                   # index it
    ...then launch app.py with CSM_SNAPSHOT to capture the panes.

Deterministic: a fixed seed means the same corpus (and the same charts) every run.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 20260804
# Anchor "now" so the daily-spend chart is always full and reproducible.
NOW = datetime(2026, 8, 4, 18, 0, 0, tzinfo=timezone.utc)

# (cwd, [ (model, weight) ], sessions, richness) — richness scales tokens & message count.
PROJECTS = [
    ("/Users/dev/acme-web",        [("claude-opus-4-8", 5), ("claude-sonnet-5", 3)], 6, 1.4),
    ("/Users/dev/billing-api",     [("claude-opus-4-8", 4), ("claude-haiku-4-5", 3)], 5, 1.2),
    ("/Users/dev/infra-terraform", [("claude-sonnet-5", 5), ("claude-haiku-4-5", 2)], 4, 0.9),
    ("/Users/dev/ml-pipeline",     [("claude-opus-4-8", 3), ("claude-fable-5", 2)], 3, 1.1),
    ("/Users/dev/docs-site",       [("claude-haiku-4-5", 4), ("claude-sonnet-5", 2)], 3, 0.6),
]

PROMPTS = [
    "Refactor the auth middleware to use short-lived JWTs with rotation.",
    "The nightly ETL job is timing out on the orders table — help me profile it.",
    "Add optimistic locking to the invoice writer and cover it with tests.",
    "Terraform plan shows a forced replacement on the RDS instance — why?",
    "Write a migration to backfill the `currency` column, idempotently.",
    "Set up a GitHub Action that runs the test suite on every PR.",
    "Explain why this recursive CTE returns duplicate rows.",
    "Trim the Docker image — it's 1.8GB and most of it is build tooling.",
    "Draft the API reference page for the /webhooks endpoints.",
    "Vectorize the feature-extraction loop; it's the training bottleneck.",
]

REPLIES = [
    ("Auth middleware — rotating JWTs",
     "Here's the plan. We'll issue a **short access token** (15 min) plus a rotating "
     "refresh token stored server-side:\n\n"
     "1. On login, mint both and persist a hash of the refresh token.\n"
     "2. On refresh, verify the hash, then *rotate* — issue a new pair and revoke the old.\n"
     "3. Reuse of a revoked refresh token trips the reuse-detection and kills the session.\n\n"
     "```python\n"
     "def rotate(refresh: str) -> TokenPair:\n"
     "    row = store.pop(hash(refresh))      # single-use\n"
     "    if row is None:\n"
     "        raise ReuseDetected()           # someone replayed an old token\n"
     "    return issue(row.user_id)\n"
     "```\n\n"
     "The reuse check is the load-bearing part — without single-use refresh, rotation "
     "buys you nothing."),
    ("Profiling the ETL timeout",
     "The `orders` scan is doing a **sequential scan** because the `status` predicate "
     "isn't sargable. Two fixes, cheapest first:\n\n"
     "- Add a partial index on `status` where `status = 'pending'`.\n"
     "- Batch the update in chunks of 5k rows so the transaction doesn't hold a lock "
     "for the whole table.\n\n"
     "Run `EXPLAIN (ANALYZE, BUFFERS)` after the index — you should see the seq scan "
     "collapse to an index-only scan."),
    ("Idempotent backfill migration",
     "Make it safe to re-run by keying on rows that are still `NULL`:\n\n"
     "```sql\n"
     "UPDATE invoices SET currency = 'USD'\n"
     "WHERE currency IS NULL;\n"
     "```\n\n"
     "Wrap it so a partial failure leaves no half-done state, and log the affected "
     "row count so a second run visibly reports `0`."),
    ("Slimming the image",
     "You're shipping the whole build toolchain. Move to a **multi-stage build** and "
     "copy only the compiled artifact into a slim runtime — that alone takes this from "
     "1.8GB to about 240MB. Pin the base by digest so the size can't drift underneath "
     "you."),
]


def _uuid(rng: random.Random) -> str:
    h = "%032x" % rng.getrandbits(128)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _usage(rng: random.Random, richness: float) -> dict:
    scale = richness
    return {
        "input_tokens": int(rng.randint(1800, 7200) * scale),
        "output_tokens": int(rng.randint(400, 2400) * scale),
        "cache_creation": {
            "ephemeral_5m_input_tokens": int(rng.randint(6000, 34000) * scale),
            "ephemeral_1h_input_tokens": int(rng.randint(0, 6000) * scale),
        },
        "cache_read_input_tokens": int(rng.randint(18000, 110000) * scale),
    }


def _write_session(rng, root: Path, cwd: str, models, richness: float,
                   start: datetime, hero: bool, throwaway: bool) -> None:
    enc = cwd.replace("/", "-").replace(".", "-")
    proj_dir = root / "projects" / enc
    proj_dir.mkdir(parents=True, exist_ok=True)
    sid = _uuid(rng)
    path = proj_dir / f"{sid}.jsonl"

    weighted = [m for m, w in models for _ in range(w)]
    exchanges = 1 if throwaway else (rng.randint(8, 14) if hero else rng.randint(3, 9))
    t = start
    lines: list[str] = []
    title = None

    for i in range(exchanges):
        prompt = rng.choice(PROMPTS)
        lines.append(json.dumps({
            "type": "user", "timestamp": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cwd": cwd, "gitBranch": "main", "version": "2.1.0",
            "message": {"role": "user", "content": prompt},
        }))
        t += timedelta(seconds=rng.randint(8, 40))

        model = rng.choice(weighted)
        rtitle, rbody = rng.choice(REPLIES)
        if title is None:
            title = rtitle
        lines.append(json.dumps({
            "type": "assistant", "timestamp": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cwd": cwd,
            "message": {
                "role": "assistant", "model": model,
                "content": [{"type": "text", "text": rbody}],
                "usage": _usage(rng, richness * (1.6 if hero else 1.0)),
            },
        }))
        t += timedelta(minutes=rng.randint(1, 9))

    lines.append(json.dumps({"type": "ai-title", "aiTitle": title or "Working session"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: make_fixture.py <output-root>", file=sys.stderr)
        return 2
    root = Path(sys.argv[1]).expanduser()
    if root.exists() and any(root.iterdir()) and root.name not in ("fixture", "purser-fixture"):
        print(f"refusing to write into non-empty {root} (name it *fixture)", file=sys.stderr)
        return 2
    (root / "projects").mkdir(parents=True, exist_ok=True)

    rng = random.Random(SEED)
    for cwd, models, n_sessions, richness in PROJECTS:
        for s in range(n_sessions):
            days_ago = rng.randint(0, 23)
            start = NOW - timedelta(days=days_ago,
                                    hours=rng.randint(0, 8), minutes=rng.randint(0, 59))
            hero = (cwd.endswith("acme-web") and s == 0)   # one rich session to feature
            _write_session(rng, root, cwd, models, richness, start,
                           hero=hero, throwaway=False)

    # A few throwaways so Cleanup has obvious triage candidates.
    for s in range(4):
        start = NOW - timedelta(days=rng.randint(5, 20))
        _write_session(rng, root, "/Users/dev/scratch",
                       [("claude-haiku-4-5", 1)], 0.4, start, hero=False, throwaway=True)

    n = len(list((root / "projects").rglob("*.jsonl")))
    print(f"wrote {n} sessions to {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
