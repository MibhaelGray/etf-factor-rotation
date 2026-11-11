#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

# One IB job at a time: a later job waits for the one running. flock on Linux, lockf on macOS. Paper jobs
# use the paper gateway and have their own lock, so a long option pull can't make the 3:45 PM exits miss the
# market-on-close cutoff.
case "${1:-}" in paper-*) lock=/tmp/fr-paper.lock ;; intraday-*) lock=/tmp/fr-intraday.lock ;; pass-*) lock=/tmp/fr-pass.lock ;; *) lock=/tmp/fr-ib.lock ;; esac
if [ -z "${FR_LOCKED:-}" ]; then
  export FR_LOCKED=1
  if command -v flock >/dev/null; then exec flock "$lock" "$0" "$@"; fi
  exec lockf -k "$lock" "$0" "$@"
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

# Three tries, half a minute apart.
retry() {
  local n
  for n in 1 2 3; do
    "$@" && return 0
    [ "$n" = 3 ] || sleep "${FR_RETRY_WAIT:-30}"
  done
  return 1
}

case "$1" in
  options-all)  exec uv run fr options --group all ;;
  options-etfs) exec uv run fr options --group etfs ;;
  bars)         exec uv run fr bars ;;
  paper-open|paper-check|paper-close|paper-sync)
    # Nothing to do until the paper account is set up in .env (README, "Paper trading").
    [ -n "${FR_PAPER_ACCOUNT:-}" ] || { echo "FR_PAPER_ACCOUNT not set; skipping $1" >&2; exit 0; }
    uv run fr trading-day || exit 0
    exec uv run fr paper "${1#paper-}" ;;
  pass-pre-close)
    uv run fr trading-day || exit 0
    exec uv run fr pass "${1#pass-}" --shadow ;;
  intraday-tick)
    uv run fr trading-day || exit 0
    exec uv run fr intraday tick ;;
  nightly)
    uv run fr trading-day || exit 0
    # The repo may be a working checkout with edits in progress: stash them around the pull, and commit only
    # the files this job writes, whatever else is staged.
    # A Mac that has just woken may have no network yet. A failed pull costs at most someone else's newer journal
    # files, and tonight's brief and screen are still needed, so it must not stop the job.
    if ! retry git pull --rebase --autostash --quiet; then
      echo "git pull failed; building tonight's files from this checkout" >&2
      if [ -d "$(git rev-parse --git-path rebase-merge)" ] || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then git rebase --abort; fi
    fi
    # Names outside the standard universe (basket members, the paper book's legs): their daily bars, in case
    # the 4:30 PM pull ran before a proposal that holds them was recorded.
    uv run fr bars --pending || echo "bars for names outside the universe failed; carrying on" >&2
    uv run fr earnings || echo "earnings dates failed; keeping the last pull" >&2
    uv run fr brief
    uv run fr gaps || echo "earnings gap table failed; keeping the last one" >&2
    uv run fr scan || echo "stock scan failed; run it by hand tonight" >&2
    uv run fr screen || echo "screen failed; run it by hand tonight" >&2
    written=()
    for d in journal/briefs journal/scans journal/screens journal/paper journal/reference/earnings.json journal/reference/earnings_gaps.json; do [ -e "$d" ] && written+=("$d"); done
    git add -- "${written[@]}"
    git diff --cached --quiet -- "${written[@]}" ||
      git commit --quiet -m "Journal $(TZ=America/New_York date +%F): brief, screen and paper book" -- "${written[@]}"
    retry git push --quiet
    ;;
  *) echo "unknown job $1" >&2; exit 2 ;;
esac
