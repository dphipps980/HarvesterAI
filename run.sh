#!/bin/bash
# HarvesterAI Web - Start server
# Run with: bash run.sh
# Or for production: bash run.sh --port 8080

PORT="${2:-7860}"
HOST="${1:-0.0.0.0}"

cd "$(dirname "$0")"

if [ ! -d venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
  venv/bin/pip install -r requirements.txt -q
fi

echo "Starting HarvesterAI on http://${HOST}:${PORT}"
echo "Open http://localhost:${PORT} in your browser"
echo ""
venv/bin/uvicorn app:app --host "${HOST}" --port "${PORT}" --workers 1
