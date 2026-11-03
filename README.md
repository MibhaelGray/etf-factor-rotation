# ETF Factor Rotation

Collects option chains and daily bars for the largest ETFs and their top constituents from IBKR, turns them into positioning, volatility and technical signals, and keeps a write-once journal of nightly factor pair ideas with a running scorecard.

## Pieces

Everything runs on the server. The Mac only reads the brief and records the ideas.

| Command | Runs on | When (ET, trading days) | Writes |
|---|---|---|---|
| `fr options --group all` | server | 10:00, 15:30 | `options_raw/`, `options_underlying/`, `exposures/`, `exposures_by_strike/` |
| `fr options --group etfs` | server | 11:00–14:00 hourly | same, ETFs only |
| `fr bars` | server | 16:30 | `bars_daily/` |
| `deploy/run.sh nightly` | server | 19:00 | runs `fr score` and `fr brief`, commits `journal/`, pushes |
| `fr record` | Mac | ~19:30 | `journal/ideas/`, committed and pushed |
| `fr scorecard` | anywhere | any time | prints the scorecard |

Market data lives on the server's disk under `FR_DATA_ROOT`. The journal (briefs, ideas, scores) travels through GitHub: the server pushes scores and briefs, and the Mac pulls them, records the ideas and pushes those back.

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

## Nightly loop

At 19:00 the server scores last session's ideas, builds tonight's brief and pushes both. Then, on the Mac:

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
   "thesis": "...", "invalidation": "...", "key_signals": ["..."]}]}
```

Each idea is dollar-neutral, entered at the next open and exited at the close. Close-to-close is recorded alongside.

## Research engine

`src/factor_rotation/research/` is the systematic side.
- **Data:** a full-history panel of every ETF plus VIX and crude (Yahoo, from inception).
- **Spreads:** 47 dollar-neutral pairs (style pairs, and each sector, industry and cross-asset fund against SPY).
- **Signals:** a library of pure signal functions with fixed prior signs.
- **Backtester:** decides at the close and trades at the next open, sizes for equal risk with a book volatility target, uses a no-trade band and per-ETF costs.
- **Trial registry:** every backtest appends to `journal/research/trials.jsonl`, so deflated Sharpe ratios account for everything tried.

```bash
uv run fr research-fetch          # rebuild the panel
uv run fr research-run            # backtest every signal and the blend (logs trials)
uv run fr book                    # record tonight's systematic book (write-once)
```

The incumbent book (`research/book.py`) is recorded nightly in `journal/book/` and scored open to close in `journal/book_scores/`, so the discretionary ideas always have a systematic benchmark. Periods: train to 2014, validate 2015–2022, holdout from 2023; only use the holdout to confirm a result, never to tune one.

## Server setup (Ubuntu 24.04)

1. **IBKR login:** create a second username in Client Portal (Settings → Users & Access Rights) and subscribe it to US options (OPRA) and US equity market data. Its own subscriptions mean your own TWS or phone sessions never cut off the collector. Expect an IB Key approval on your phone about once a week, when the gateway's session expires.
2. **Software:** install Docker and `uv`, then clone this repo to `/opt/factor-rotation`. Add a GitHub deploy key with write access so the nightly job can push, and set `git config user.name` and `user.email` in the repo.
3. **Settings:** copy `.env.example` to `.env` and fill in the login.
4. **Start the gateway:** `docker compose -f deploy/docker-compose.yml up -d`
5. **Install the schedules:**
   ```bash
   sudo cp deploy/systemd/* /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now fr-options-all.timer fr-options-etfs.timer fr-bars.timer fr-nightly.timer
   ```
6. **First run:** `uv run fr bars` (backfills 5 years of bars), then `uv run fr options --symbols SPY,QQQ` to check permissions and timing.

Logs: `journalctl -u 'fr@*'`.

## Backup

The server's disk holds the only copy of the market data, and past option chains can't be re-downloaded. Copy it to the Mac now and then:

```bash
rsync -az fr-server:/opt/factor-rotation/data/ ~/Documents/"ETF Factor Rotation"/data/
```
