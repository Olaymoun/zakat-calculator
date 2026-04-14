#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

# ── Python check ────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "❌  Python 3 is required. Install it from https://python.org"
  exit 1
fi

# ── Virtual env ─────────────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
  echo "→  Creating virtual environment…"
  python3 -m venv venv
fi
source venv/bin/activate

# ── Dependencies ─────────────────────────────────────────────────────────────
echo "→  Installing Python dependencies…"
pip install -q -r requirements.txt

# ── .env setup ───────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "⚠️   A .env file was created. Open it and add your FMP_API_KEY,"
  echo "    or enter the key in the app's Settings panel."
  echo ""
fi
export $(grep -v '^#' .env | xargs 2>/dev/null) 2>/dev/null || true

# ── Launch ───────────────────────────────────────────────────────────────────
echo ""
echo "✅  Starting Zakat Calculator → http://127.0.0.1:8000"
echo "    Press Ctrl+C to stop."
echo ""
python3 main.py
