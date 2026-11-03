#!/usr/bin/env bash
set -euo pipefail
PIWATCH_SOURCE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec /usr/bin/python3 "$PIWATCH_SOURCE/deployment/ntfy_cutover.py" --source "$PIWATCH_SOURCE" "$@"
