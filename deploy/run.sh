#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
case "$1" in
  options-all)  exec uv run fr options --group all ;;
  options-etfs) exec uv run fr options --group etfs ;;
  bars)         exec uv run fr bars ;;
  nightly)
    uv run fr trading-day || exit 0
    git pull --rebase --quiet
    uv run fr score
    uv run fr brief
    git add journal
    git diff --cached --quiet || git commit --quiet -m "Journal $(TZ=America/New_York date +%F): scores and brief"
    git push --quiet
    ;;
  *) echo "unknown job $1" >&2; exit 2 ;;
esac
