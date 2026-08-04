#!/usr/bin/env python3
"""One-off probe: confirm the live usage endpoint + dump its response SHAPE.

Run:  .venv/bin/python tools/probe_usage.py

Reads your Claude Code OAuth token from the login Keychain (macOS may prompt once to
allow access) and calls the usage endpoint the CLI uses. It prints the HTTP status and
a structural outline of the response — NEVER the token, and it truncates long string
values — so it's safe to share the output.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request

KEYCHAIN_SERVICE = "Claude Code-credentials"
CANDIDATE_URLS = [
    "https://api.anthropic.com/api/oauth/usage",
    "https://console.anthropic.com/api/oauth/usage",
    "https://api.anthropic.com/api/oauth/claude_cli/usage",
]
BETA_HEADER = "oauth-2025-04-20"


def read_token() -> str | None:
    try:
        blob = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=20)
    except Exception as exc:
        print(f"keychain read failed: {exc}")
        return None
    if blob.returncode != 0 or not blob.stdout.strip():
        print(f"keychain read returned nothing (rc={blob.returncode}); "
              f"stderr: {blob.stderr.strip()[:120]}")
        return None
    raw = blob.stdout.strip()
    # The item is JSON; the token lives under claudeAiOauth.accessToken (or accessToken).
    try:
        d = json.loads(raw)
        tok = (d.get("claudeAiOauth") or {}).get("accessToken") or d.get("accessToken")
        if tok:
            return tok
    except ValueError:
        pass
    # Maybe the stored secret is the bare token.
    if raw.startswith("sk-ant-") or len(raw) > 40:
        return raw
    print("couldn't locate an access token inside the keychain item")
    return None


def outline(value, depth=0, key=""):
    """Print a structural outline: keys, types, sample scalar values (truncated)."""
    pad = "  " * depth
    if isinstance(value, dict):
        print(f"{pad}{key + ': ' if key else ''}{{}}  ({len(value)} keys)")
        for k, v in list(value.items())[:40]:
            outline(v, depth + 1, k)
    elif isinstance(value, list):
        print(f"{pad}{key + ': ' if key else ''}[{len(value)}]")
        if value:
            outline(value[0], depth + 1, "[0]")
    else:
        s = str(value)
        # numbers / short scalars are useful (usage %, reset times); long strings truncated
        shown = s if len(s) <= 60 else s[:57] + "…"
        print(f"{pad}{key}: {shown}")


def main() -> int:
    token = read_token()
    if not token:
        print("\nNo token — cannot probe. (Is Claude Code logged in on this machine?)")
        return 1
    print(f"token: OK ({len(token)} chars, prefix {token[:8]}…)\n")

    for url in CANDIDATE_URLS:
        print(f"=== GET {url} ===")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA_HEADER,
            "anthropic-version": "2023-06-01",
            "User-Agent": "purser-probe/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", "replace")
                print(f"HTTP {resp.status}, {len(body)} bytes")
                try:
                    data = json.loads(body)
                    print("response shape:")
                    outline(data)
                except ValueError:
                    print("body (first 300 chars):", body[:300])
                print("\n^ this endpoint works — share the shape above.\n")
                return 0
        except urllib.error.HTTPError as e:
            print(f"HTTP {e.code} {e.reason}; body: {e.read()[:160].decode('utf-8','replace')}")
        except Exception as e:
            print(f"error: {type(e).__name__}: {e}")
        print()

    print("None of the candidate URLs worked. Share the output above so I can adjust.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
