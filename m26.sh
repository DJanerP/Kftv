#!/bin/bash
# Alias para ejecutar cualquier comando de Mundia2026
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/main.py" "$@"
