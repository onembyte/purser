#!/bin/bash
# Bootstrap the Purser dev environment.
# PyObjC 12.x ships universal2 wheels for cp312/cp313/cp314; if a future Python
# outruns the wheels, PY_FALLBACK is used instead of attempting a source build.
set -euo pipefail
cd "$(dirname "$0")"

PY_PREFERRED="${PY_PREFERRED:-/opt/homebrew/bin/python3}"
PY_FALLBACK="${PY_FALLBACK:-/opt/homebrew/opt/python@3.13/bin/python3.13}"

pick_python() {
    for py in "$PY_PREFERRED" "$PY_FALLBACK"; do
        [ -x "$py" ] || continue
        tag=$("$py" -c 'import sys;print(f"cp{sys.version_info.major}{sys.version_info.minor}")')
        if "$py" -m pip download --quiet --no-deps --only-binary=:all: \
               --dest /tmp/csm-wheelprobe pyobjc-core >/dev/null 2>&1; then
            echo "$py"; return 0
        fi
        echo "  no pyobjc wheel for $tag ($py), trying next..." >&2
    done
    echo "ERROR: no Python with available pyobjc wheels" >&2; exit 1
}

PY=$(pick_python)
echo "==> Using $PY ($("$PY" -V))"

[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-WebKit

.venv/bin/python - <<'EOF'
import objc, sqlite3
import AppKit, WebKit
sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(x)")
print(f"==> pyobjc {objc.__version__}, sqlite {sqlite3.sqlite_version} (FTS5 ok)")
EOF

echo "==> Ready.  Run:  .venv/bin/python app.py"
