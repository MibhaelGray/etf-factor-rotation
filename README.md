# ETF Factor Rotation

Collects option chains and daily bars for the largest ETFs and their top constituents from IBKR, turns them into positioning, volatility and technical signals, and keeps a write-once journal of nightly factor pair ideas with a running scorecard.

## Pieces

Data collection runs on the collector: a second Mac that stays on, plugged in and logged in, with the IB Gateway in Docker. The main Mac only reads the brief, runs the research and records the ideas.

Every collected dataset comes from IBKR, so the pipeline has one data source. Web research feeds the nightly dossier as context, not as stored data.

| Command | Runs on | When (ET, trading days) | Writes |
|---|---|---|---|
| `fr options --group all` | collector | 10:00, 15:30 | `options_raw/`, `options_underlying/`, `exposures/`, `exposures_by_strike/` |
| `fr options --group etfs` | collector | 11:00–14:00 hourly | same, ETFs only |
| `fr bars` | collector | 16:30 | `bars_daily/` |
| `deploy/run.sh nightly` | collector | 19:00 | runs `fr intraday`, `fr score` and `fr brief`, commits `journal/`, pushes |
| `fr intraday` | collector | in the nightly job | `journal/intraday/`: the session's 15-minute bars (04:00–16:00) for the idea legs and SPY |
| `fr record` | Mac | ~19:30 | `journal/ideas/`, committed and pushed |
| `fr check` | Mac | 08:30–09:25 | `journal/checks/`: the pre-open stands / stand-down decision per idea; refused after the open |
| `fr scorecard` | anywhere | any time | prints the scorecard |
| `fr ledger` | Mac | after `git pull` | every idea and its result as JSON rows for the [Factor Rotation Ledger](https://claude.ai/artifact/TVknCZmSXjToEPbmMGPSKv) dashboard |

Market data lives on the collector's disk under `FR_DATA_ROOT` (default `data/` in the repo). The journal (briefs, ideas, scores) travels through GitHub: the collector pushes scores and briefs, and the main Mac pulls them, records the ideas and pushes those back.

## Options data

For each underlying, the collector takes:
- every expiry within 8 days
- the monthly expiries out to 120 days for ETFs or 75 days for stocks
- the weekly expiries out to 35 days, while there's room under the cap of 12 expiries

Strikes are kept within ±2.5 standard deviations of spot, using the underlying's IV, with a floor of 3% and a cap of 15% for ETFs or 20% for stocks. Each contract stores:
- bid, ask and sizes
- last price, volume and open interest
- IBKR's model greeks

`fr options` then computes our own greeks for every contract. It implies a forward from put-call parity near the money, solves IV from the quote mid on the out-of-the-money side of each strike, and uses those to compute delta, gamma, vega, theta, vanna and charm. From those it computes, per underlying:
- **Positioning:** total and 0DTE gamma exposure (GEX), the gamma flip level, call and put walls, vanna and charm exposure
- **Volatility surface:** ATM IV at 30 and 90 days, term slope, 25-delta skew
- **Flow:** put/call ratios by volume and open interest, premium traded, 0DTE share, and unusual volume (volume above open interest)

GEX assumes dealers are long calls and short puts. Open interest updates once a day, overnight.

## Forced flows

The brief's `forced_flows` section estimates trading that has to happen regardless of anyone's view, from the same IBKR data:
- **Calendar:** monthly and quarterly option expiries, VIX expiry, month and quarter ends, in sessions from tonight.
- **Dealer gamma by expiry:** each upcoming expiry's share of gamma and delta-weighted open interest, for the index ETFs and largest names (`expiry_profile` in `exposures/`).
- **Leveraged ETFs:** the close-of-day hedge of 18 leveraged and inverse funds, grouped by index: net assets × L(L−1) × the index return. Net assets come from IBKR into `journal/reference/leveraged_assets.json` (refreshed weekly) and are scaled by each fund's close.
- **Month- and quarter-end rebalancing:** drift of a 60/40 SPY/AGG portfolio, ranked against completed periods.
- **Vol-control funds:** exposure of a 10% vol-target fund on SPY (EWMA vol), and how a −3% to +2% session would change it.
- **Trend followers:** a 20/60/120/250-day trend score per asset and the price where each lookback flips in the next session.
- **Borrow:** shortable shares and IBKR's shortable indicator from the underlying snapshot (generic tick 236), compared with five sessions earlier.

The leveraged funds and AGG are pulled as daily bars only; they aren't option underlyings or idea legs (`universe.idea_universe()`).

## Nightly loop

At 19:00 the collector scores last session's ideas, builds tonight's brief and pushes both. Then, on the Mac:

```bash
git pull                          # brings in journal/briefs/<today>.json and the new scores
uv run fr record draft.json       # record 1-3 ideas for the next session (write-once)
git add journal && git commit -m "Ideas for <next trading day>" && git push
uv run fr scorecard
```

Draft format:

```json
{"as_of": "2026-11-02", "macro_context": "...", "ideas": [
  {"long": "MTUM", "short": "USMV", "factor": "momentum", "confidence": 4,
   "thesis": "...", "invalidation": "...", "key_signals": ["..."],
   "stand_down_if": ["MTUM gaps more than 1 ATR above USMV pre-market", "..."]}]}
```

Each idea is dollar-neutral, entered at the next open and exited at the close. Close-to-close is recorded alongside.

Before the open, `fr check` records whether each idea stands or is stood down under its own `stand_down_if` rules. The ideas never change; the scorecard reports the full book and the book after the check side by side.

## Collector setup (macOS)

1. **IBKR login:** create a second username in Client Portal (Settings → Users & Access Rights) and subscribe it to US options (OPRA) and US equity market data. Its own subscriptions mean your own TWS or phone sessions never cut off the collector. Expect an IB Key approval on your phone about once a week, when the gateway's session expires.
2. **Software:** install Docker Desktop (Settings → General → Start Docker Desktop when you sign in), `uv` and git. Clone this repo, set `git config user.name` and `user.email` in it, and give the Mac an SSH key with write access to the GitHub repo and no passphrase, so the nightly job can push unattended.
3. **Settings:** copy `.env.example` to `.env` and fill in the login.
4. **Start the gateway:** `docker compose -f deploy/docker-compose.yml up -d`. It restarts with Docker Desktop.
5. **Keep it awake on power.** Jobs that come due while the Mac sleeps run late, when it wakes, so the snapshots would be mistimed. In System Settings, turn on automatic login, and under Battery → Options turn on "Prevent automatic sleeping on power adapter when the display is off". Or from a terminal:
   ```bash
   sudo pmset -c sleep 0 disksleep 0
   ```
   Leave the lid open, or closed with an external display attached; a closed lid without one sleeps regardless. Each job also runs under `caffeinate`, so the Mac stays awake until it finishes.
6. **Install the schedules:** `python3 deploy/launchd/install.py`. It converts the New York schedule to the Mac's time zone (rerun it if that changes) and installs one launchd agent per job. `--dry-run` shows the plists without installing, `--uninstall` removes them.
7. **First run:** `uv run fr bars` (backfills 5 years of bars), then `uv run fr options --symbols SPY,QQQ` to check permissions and timing.

Logs: `~/Library/Logs/factor-rotation/<job>.log`. To run a job now: `launchctl kickstart gui/$(id -u)/com.factor-rotation.nightly`.

A Linux box works too: `deploy/systemd/` holds the same schedule as systemd timers, with the repo at `/opt/factor-rotation`.

## Backup

The collector's disk holds the only copy of the market data, and past option chains can't be re-downloaded. Copy it to the main Mac now and then (turn on System Settings → General → Sharing → Remote Login on the collector first):

```bash
rsync -az collector.local:<repo path>/data/ ~/Documents/"ETF Factor Rotation"/data/
```
