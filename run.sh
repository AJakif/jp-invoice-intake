#!/usr/bin/env bash
# Single command to start the whole pipeline: mock accounting API + invoice
# intake. Run from the repo root: ./run.sh [--process-review]
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
python3 --version >/dev/null 2>&1 || PY=python

echo "Starting mock accounting API..."
"$PY" accounting_api.py &
API_PID=$!
trap 'kill "$API_PID" 2>/dev/null || true' EXIT

echo "Waiting for accounting API to become healthy..."
healthy=0
for _ in $(seq 1 20); do
  if curl -sf http://localhost:8080/health >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 0.5
done

if [ "$healthy" -ne 1 ]; then
  echo "Accounting API did not become healthy in time." >&2
  exit 1
fi

echo "Running invoice intake pipeline..."
"$PY" src/pipeline.py --invoices-dir invoices "$@"
