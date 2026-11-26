#!/usr/bin/env bash
set -euo pipefail
scripts=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec "$scripts/../.venv/bin/python" "$scripts/reconstruct_and_calibrate.py" "$@"
