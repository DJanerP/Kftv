#!/bin/bash
# Mundia2026 — Arrancar el scheduler
# Usa el venv propio del proyecto

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Mundia2026 — Arrancar scheduler"
echo "Logs en: $SCRIPT_DIR/logs/mundia2026.log"
echo ""

exec "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/main.py" daemon
