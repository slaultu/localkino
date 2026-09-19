#!/usr/bin/env bash
# KinoPub Offline launcher — no dependencies beyond system Python 3.
set -euo pipefail
cd "$(dirname "$0")"
PY="$(command -v python3 || echo /usr/bin/python3)"
exec "$PY" server.py "$@"
