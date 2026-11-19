#!/usr/bin/env bash
# One-time setup (Mac/Linux): creates .venv and installs Python packages.
set -e
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null || { echo "ffmpeg not found. Mac: brew install ffmpeg | Linux: sudo apt install ffmpeg"; exit 1; }
PY=$(command -v python3.12 || command -v python3.11 || command -v python3.10 || command -v python3)
"$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "Ready. Try: .venv/bin/python scripts/clipper.py live https://www.twitch.tv/<channel>"
