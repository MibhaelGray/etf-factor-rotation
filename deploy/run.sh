#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# One IB job at a time: a later job waits for the one running. flock on Linux, lockf on macOS.
if [ -z "${FR_LOCKED:-}" ]; then
  export FR_LOCKED=1
  if command -v flock >/dev/null; then exec flock /tmp/fr-ib.lock "$0" "$@"; fi
  exec lockf -k /tmp/fr-ib.lock "$0" "$@"
fi

# Keep the Mac awake until this job ends.
caffeinate -i -w $$ &

# Collector settings (FR_*) from .env; the gateway login in it is only for docker compose.
if [ -f .env ]; then
  while IFS='=' read -r key value; do
    value="${value%\"}"; value="${value#\"}"
    case "$key" in FR_*) export "$key=$value" ;; esac
  done < .env
fi

case "$1" in
  options-all)  exec uv run fr options --group all ;;
  options-etfs) exec uv run fr options --group etfs ;;
  bars)         exec uv run fr bars ;;
  nightly)
    uv run fr trading-day || exit 0
    # The repo may be a working checkout with edits in progress: stash them around the pull, and commit only
    # the files this job writes, whatever else is staged.
    git pull --rebase --autostash --quiet
    uv run fr intraday || echo "intraday bars failed; scoring anyway" >&2
    # Basket members outside the standard universe: their daily bars, in case the 16:30 pull ran before
    # the ideas that hold them reached this checkout.
    uv run fr bars --pending || echo "basket bars failed; scoring anyway" >&2
    uv run fr score
    uv run fr earnings || echo "earnings dates failed; keeping the last pull" >&2
    uv run fr brief
    uv run fr gaps || echo "earnings gap table failed; keeping the last one" >&2
    uv run fr scan || echo "stock scan failed; run it by hand tonight" >&2
    uv run fr screen || echo "screen failed; run it by hand tonight" >&2
    written=()
    for d in journal/scores journal/morning/scores journal/outcomes journal/morning/outcomes journal/briefs journal/intraday journal/scans journal/screens journal/reference/earnings.json journal/reference/earnings_gaps.json; do [ -e "$d" ] && written+=("$d"); done
    git add -- "${written[@]}"
    git diff --cached --quiet -- "${written[@]}" ||
      git commit --quiet -m "Journal $(TZ=America/New_York date +%F): scores and brief" -- "${written[@]}"
    git push --quiet
    ;;
  *) echo "unknown job $1" >&2; exit 2 ;;
esac
