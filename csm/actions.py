"""Side-effecting operations: resume in Terminal, clipboard, Finder, Trash."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import AppKit

from csm import config


def resume_command(session_id: str, cwd: str | None) -> tuple[str, str, bool]:
    """-> (shell_command, effective_cwd, cwd_was_missing).

    `claude` is left unqualified: Terminal's `do script` runs a login shell, which
    sources the user's profile and puts ~/.local/bin on PATH. Hard-coding the absolute
    path would break if the install moves.
    """
    missing = not (cwd and os.path.isdir(cwd))
    effective = os.path.expanduser("~") if missing else cwd
    cmd = f"cd {shlex.quote(effective)} && claude --resume {shlex.quote(session_id)}"
    return cmd, effective, missing


def _osascript(script: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(["osascript", "-"], input=script, text=True,
                           capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if p.returncode != 0:
        return False, (p.stderr or "").strip()
    return True, (p.stdout or "").strip()


def resume_in_terminal(session_id: str, cwd: str | None) -> dict:
    """Open a NEW Terminal window running `claude --resume <id>` in the session's cwd."""
    cmd, effective, missing = resume_command(session_id, cwd)

    # AppleScript string literal: escape backslashes first, then double quotes.
    # shlex.quote() emits single quotes, which need no escaping inside one.
    literal = cmd.replace("\\", "\\\\").replace('"', '\\"')
    ok, err = _osascript(
        'tell application "Terminal"\n'
        "  activate\n"
        f'  do script "{literal}"\n'
        "end tell\n"
    )
    if not ok:
        # -1743 is the Automation permission denial; anything else is a real failure.
        if "-1743" in err or "not allowed" in err.lower():
            raise PermissionError(
                "macOS blocked controlling Terminal. Allow it under System Settings → "
                "Privacy & Security → Automation, then try again."
            )
        raise RuntimeError(err or "osascript failed")
    return {"command": cmd, "cwd": effective, "cwdMissing": missing}


def copy_to_clipboard(text: str) -> dict:
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
    return {"copied": text}


def reveal_in_finder(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    url = AppKit.NSURL.fileURLWithPath_(str(p))
    AppKit.NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([url])
    return {"revealed": str(p)}


def _assert_inside_claude_root(path: Path) -> Path:
    """Refuse to touch anything outside ~/.claude (or $CSM_CLAUDE_ROOT).

    Belt and braces: paths come from our own index, but this is the last gate before
    an irreversible-looking operation, and a symlink inside the tree could otherwise
    point anywhere. Compares fully resolved paths.
    """
    root = config.CLAUDE_ROOT.resolve()
    resolved = path.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"refusing to touch a path outside {root}: {path}")
    return resolved


def trash_paths(paths: list[str]) -> dict:
    """Move paths to the Trash (recoverable), never unlink.

    NSFileManager.trashItemAtURL is synchronous and gives a per-item result, unlike
    NSWorkspace.recycleURLs which is async — easier to report honestly.
    """
    fm = AppKit.NSFileManager.defaultManager()
    trashed, failed = [], []
    for p in paths:
        try:
            resolved = _assert_inside_claude_root(Path(p))
        except (ValueError, OSError) as exc:
            failed.append({"path": p, "error": str(exc)})
            continue
        if not resolved.exists():
            continue
        url = AppKit.NSURL.fileURLWithPath_(str(resolved))
        ok, _new_url, err = fm.trashItemAtURL_resultingItemURL_error_(url, None, None)
        if ok:
            trashed.append(str(resolved))
        else:
            failed.append({"path": str(resolved),
                           "error": str(err.localizedDescription()) if err else "failed"})
    return {"trashed": trashed, "failed": failed}


def open_path(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.fileURLWithPath_(str(p)))
    return {"opened": str(p)}
