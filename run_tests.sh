#!/usr/bin/env bash
# Run the test suite. Nothing here touches your real settings or library.
set -euo pipefail
cd "$(dirname "$0")"
exec /usr/bin/python3 -m unittest discover -s tests -p 'test_*.py' -v
