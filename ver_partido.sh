#!/bin/bash
# ver_partido.sh — envoltorio: ver_partido.sh fuentes | ver a07z [--retraso 45] | estado | parar
cd "$(dirname "$0")" && exec venv/bin/python ver_partido.py "$@"
