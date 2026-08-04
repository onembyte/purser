"""Paths, pricing, and model metadata. Single source of truth for the app."""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "Purser"
APP_TAGLINE = "Session ledger for Claude Code"
BUNDLE_DIR_NAME = "Purser"

# CSM_CLAUDE_ROOT lets the whole app be pointed at a *copy* of ~/.claude.
# Cleanup/trash work must only ever be exercised against a fixture tree until proven.
CLAUDE_ROOT = Path(os.environ.get("CSM_CLAUDE_ROOT", "~/.claude")).expanduser()

PROJECTS_DIR = CLAUDE_ROOT / "projects"       # projects/<encoded-cwd>/<uuid>.jsonl
LIVE_DIR = CLAUDE_ROOT / "sessions"           # sessions/<pid>.json  (live process registry)
SESSION_ENV_DIR = CLAUDE_ROOT / "session-env"  # session-env/<sessionId>/
TASKS_DIR = CLAUDE_ROOT / "tasks"             # tasks/<sessionId>/
JOBS_DIR = CLAUDE_ROOT / "jobs"               # jobs/<sessionId[:8]>/   (~184MB)
PLANS_DIR = CLAUDE_ROOT / "plans"             # plans/<slug>.md
HISTORY_FILE = CLAUDE_ROOT / "history.jsonl"

# CSM_APP_SUPPORT pairs with CSM_CLAUDE_ROOT so a fixture run gets its own index and
# never clobbers the real one.
APP_SUPPORT = Path(os.environ.get(
    "CSM_APP_SUPPORT",
    str(Path("~/Library/Application Support").expanduser() / BUNDLE_DIR_NAME))
).expanduser()
DB_PATH = APP_SUPPORT / "index.db"

WEB_DIR = Path(__file__).resolve().parent / "web"

# Bump to force a drop-and-rebuild of the index (it is a cache, never a source of truth).
SCHEMA_VERSION = 1

# --------------------------------------------------------------------------- pricing
# USD per million tokens. Cache write/read are fixed multiples of the input rate:
#   5m cache write = 1.25x in   |   1h cache write = 2x in   |   cache read = 0.1x in
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0
CACHE_READ_MULT = 0.1

# Longest-prefix match against message.model. Order here is irrelevant; length wins.
PRICING: dict[str, tuple[float, float]] = {
    # prefix                 (input, output)
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-mythos-preview": (10.0, 50.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-opus-4": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3-5-haiku": (1.0, 5.0),
    "claude-3-haiku": (1.0, 5.0),
}
FALLBACK_PRICING = PRICING["claude-opus-4-8"]  # unknown/future model -> opus tier

# Records with this model carry all-zero usage (interrupt/placeholder turns).
SYNTHETIC_MODEL = "<synthetic>"

DISPLAY_NAMES = {
    "claude-fable-5": "Fable 5",
    "claude-mythos-5": "Mythos 5",
    "claude-opus-4-8": "Opus 4.8",
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-6": "Opus 4.6",
    "claude-opus-4-5": "Opus 4.5",
    "claude-sonnet-5": "Sonnet 5",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-sonnet-4-5": "Sonnet 4.5",
    "claude-haiku-4-5-20251001": "Haiku 4.5",
    "claude-haiku-4-5": "Haiku 4.5",
    SYNTHETIC_MODEL: "Synthetic",
}


def price_for(model: str) -> tuple[float, float, bool]:
    """Return (input_per_mtok, output_per_mtok, is_estimated) for a model id."""
    best, best_len = None, -1
    for prefix, rates in PRICING.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = rates, len(prefix)
    if best is None:
        return (*FALLBACK_PRICING, True)
    return (*best, False)


def cost_of(model: str, in_tok: int, out_tok: int,
            cache_w5_tok: int, cache_w1h_tok: int, cache_r_tok: int) -> float:
    """USD cost of one usage record at published API list prices."""
    p_in, p_out, _ = price_for(model)
    return (
        in_tok * p_in
        + out_tok * p_out
        + cache_w5_tok * p_in * CACHE_WRITE_5M_MULT
        + cache_w1h_tok * p_in * CACHE_WRITE_1H_MULT
        + cache_r_tok * p_in * CACHE_READ_MULT
    ) / 1_000_000.0


def display_name(model: str) -> str:
    if model in DISPLAY_NAMES:
        return DISPLAY_NAMES[model]
    for prefix, name in DISPLAY_NAMES.items():
        if model.startswith(prefix):
            return name
    return model


# Cost is computed at API list prices; the user is on a subscription, so every
# surface that shows money must carry this caption.
COST_CAPTION = "API-equivalent estimate"

# --------------------------------------------------------------------------- colour
# Chart colour follows the MODEL, never its rank in the current view — otherwise
# filtering or a change in spend would repaint the surviving series. Slots index the
# validated categorical palette (see web/app.css --series-N); the order below is fixed
# and permanent, not sorted by cost.
MODEL_SLOT = {
    "claude-opus-4-8": 1,
    "claude-fable-5": 2,
    "claude-opus-4-7": 3,
    "claude-sonnet-5": 4,
    "claude-sonnet-4-6": 5,
    "claude-haiku-4-5": 6,
    "claude-opus-4-6": 7,
    "claude-opus-4-5": 8,
}
FALLBACK_SLOT = 8  # unknown/future models share the last slot rather than inventing a hue


def model_slot(model: str) -> int:
    best, best_len = None, -1
    for prefix, slot in MODEL_SLOT.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = slot, len(prefix)
    return best if best is not None else FALLBACK_SLOT
