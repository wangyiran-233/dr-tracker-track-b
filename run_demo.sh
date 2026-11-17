#!/usr/bin/env bash
# One command: create a virtual environment, install dependencies, start the dashboard.
# No API key needed: the dashboard reads the pre-built, synthetic data/cinv_demo.db.
#   bash run_demo.sh
set -euo pipefail
cd "$(dirname "$0")"

ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; }

VENV_PY=.venv/bin/python
[ -x "$VENV_PY" ] || VENV_PY=.venv/Scripts/python.exe   # Git Bash on Windows
if [ ! -x "$VENV_PY" ] || ! ok "$VENV_PY"; then
    PY=""
    for c in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$c" >/dev/null 2>&1 && ok "$c"; then PY="$c"; break; fi
    done
    [ -n "$PY" ] || { echo "Python 3.11 or newer is required (developed on 3.13)." >&2; exit 1; }
    rm -rf .venv
    echo "Creating .venv with $PY ..."
    "$PY" -m venv .venv
    VENV_PY=.venv/bin/python
    [ -x "$VENV_PY" ] || VENV_PY=.venv/Scripts/python.exe
fi

echo "Installing dependencies ..."
"$VENV_PY" -m pip install --quiet --disable-pip-version-check -r requirements.txt

unset GROQ_API_KEY   # the demo never calls the API
echo "Starting the dashboard at http://localhost:8501 (Ctrl+C to stop) ..."
exec "$VENV_PY" -m streamlit run app.py
