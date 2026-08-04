"""Live plan-usage fetch.

The `cachedUsageUtilization` snapshot in ~/.claude.json only updates when Claude Code
decides to refresh it, so it lags reality. This fetches the same data the CLI's `/usage`
shows, live, from the OAuth usage endpoint.

Design constraints that shape this file:
  * NETWORK OFF THE MAIN THREAD. A background daemon thread fetches on an interval and
    stores the result in memory; `latest()` is a non-blocking read. The menu-bar timer
    and the bridge worker both call `latest()`, never the network.
  * ONE keychain prompt. The token is read once and cached for the process; it's only
    re-read from the Keychain when a request comes back 401 (the CLI rotates it there).
  * GRACEFUL FALLBACK. Any failure — no token, network down, endpoint moved, bad shape —
    leaves `latest()` returning None so the caller uses the file cache. Never raises.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request

KEYCHAIN_SERVICE = "Claude Code-credentials"
BETA_HEADER = "oauth-2025-04-20"
CANDIDATE_URLS = [
    "https://api.anthropic.com/api/oauth/usage",
    "https://console.anthropic.com/api/oauth/usage",
    "https://api.anthropic.com/api/oauth/claude_cli/usage",
]
FETCH_INTERVAL = 30.0      # seconds between background fetches
TIMEOUT = 15.0


def _read_token() -> str | None:
    try:
        p = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    raw = p.stdout.strip()
    try:
        d = json.loads(raw)
        tok = (d.get("claudeAiOauth") or {}).get("accessToken") or d.get("accessToken")
        if tok:
            return tok
    except ValueError:
        pass
    return raw if len(raw) > 40 else None


def _find_utilization(data):
    """Locate the utilization object regardless of how the endpoint wraps it.

    The cached form is `{...: {utilization: {limits: [...], five_hour: {...}, ...}}}`.
    The endpoint may return the utilization directly, or wrapped one or two levels deep.
    Recognise it by the presence of a `limits` list or the `five_hour`/`seven_day` keys.
    """
    def looks_like(o):
        return isinstance(o, dict) and (
            isinstance(o.get("limits"), list)
            or "five_hour" in o or "seven_day" in o)

    if looks_like(data):
        return data
    if isinstance(data, dict):
        for key in ("utilization", "usage", "cachedUsageUtilization", "data"):
            v = data.get(key)
            if looks_like(v):
                return v
            if isinstance(v, dict) and looks_like(v.get("utilization")):
                return v["utilization"]
        # last resort: shallow scan
        for v in data.values():
            if looks_like(v):
                return v
    return None


class _LiveUsage:
    def __init__(self):
        self._lock = threading.Lock()
        self._util = None          # latest utilization dict
        self._fetched_ms = None    # when we fetched it (epoch ms)
        self._url = None           # the URL that worked
        self._token = None
        self._enabled = True
        self._last_error = None
        self._wake = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="csm-usage-live",
                                        daemon=True)
        self._thread.start()

    def refresh_now(self):
        self._wake.set()

    def _loop(self):
        while True:
            if self._enabled:
                try:
                    self._fetch_once()
                except Exception as exc:            # never let the thread die
                    self._last_error = f"{type(exc).__name__}: {exc}"
            self._wake.wait(FETCH_INTERVAL)
            self._wake.clear()

    # ------------------------------------------------------------------ fetch
    def _fetch_once(self):
        if self._token is None:
            self._token = _read_token()
        if not self._token:
            self._last_error = "no token in keychain"
            return

        urls = [self._url] if self._url else CANDIDATE_URLS
        for url in urls:
            ok, retry_after_reauth = self._try(url)
            if ok:
                self._url = url
                self._last_error = None
                return
            if retry_after_reauth:
                # token likely expired — the CLI keeps a fresh one in the keychain
                self._token = _read_token()
                if self._token:
                    ok, _ = self._try(url)
                    if ok:
                        self._url = url
                        self._last_error = None
                        return
        # nothing worked this round; keep whatever we last had

    def _try(self, url) -> tuple[bool, bool]:
        """-> (success, should_retry_after_reauth)."""
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}",
            "anthropic-beta": BETA_HEADER,
            "anthropic-version": "2023-06-01",
            "User-Agent": "purser/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            self._last_error = f"HTTP {e.code} at {url}"
            return (False, e.code in (401, 403))
        except Exception as e:
            self._last_error = f"{type(e).__name__} at {url}"
            return (False, False)

        try:
            data = json.loads(body)
        except ValueError:
            self._last_error = f"non-JSON from {url}"
            return (False, False)

        util = _find_utilization(data)
        if util is None:
            self._last_error = f"unrecognised shape from {url}"
            return (False, False)

        with self._lock:
            self._util = util
            self._fetched_ms = int(time.time() * 1000)
        return (True, False)

    # ------------------------------------------------------------------ read
    def latest(self):
        """-> (utilization_dict, fetched_ms) or None. Non-blocking."""
        with self._lock:
            if self._util is None:
                return None
            return self._util, self._fetched_ms

    def status(self) -> dict:
        return {"enabled": self._enabled, "url": self._url,
                "hasToken": self._token is not None, "error": self._last_error,
                "haveData": self._util is not None}


live = _LiveUsage()
