#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
case "$1" in
  options-all)  exec uv run fr options --group all ;;
  options-etfs) exec uv run fr options --group etfs ;;
  bars)         exec uv run fr bars ;;
  *) echo "unknown job $1" >&2; exit 2 ;;
esac
