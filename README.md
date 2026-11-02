# ETF Factor Rotation

Collects option chains and daily bars for the largest ETFs and their top constituents from IBKR, turns them into positioning, volatility and technical signals, and keeps a write-once journal of nightly factor pair ideas with a running scorecard.

## Pieces

| Command | Runs on | When (ET) | Writes |
|---|---|---|---|
| `fr options --group all` | AWS | 10:00, 15:30 | `options_raw/`, `options_underlying/`, `exposures/`, `exposures_by_strike/` |
| `fr options --group etfs` | AWS | 11:00–14:00 hourly | same, ETFs only |
| `fr bars` | AWS | 16:30 | `bars_daily/` |
| `fr score`, `fr brief`, `fr record` | Mac | ~19:30 | `journal/scores/`, `journal/briefs/`, `journal/ideas/` |
| `fr scorecard` | anywhere | any time | prints the scorecard |

Data lives under `FR_DATA_ROOT` (a local path or `s3://bucket/prefix`). The journal is committed to git.

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

```bash
uv run fr score                   # score last session's ideas
uv run fr brief                   # snapshot every input into journal/briefs/<today>.json
uv run fr record draft.json       # record 1-3 ideas for the next session (write-once)
uv run fr scorecard
```

Draft format:

```json
{"as_of": "2026-11-02", "macro_context": "...", "ideas": [
  {"long": "MTUM", "short": "USMV", "factor": "momentum", "confidence": 4,
   "thesis": "...", "invalidation": "...", "key_signals": ["..."]}]}
```

Each idea is dollar-neutral, entered at the next open and exited at the close. Close-to-close is recorded alongside.

## AWS setup

1. **IBKR login:** create a paper login (or a second username) for the server, and share your live market data with it (Settings → Paper Trading Account). One username can't hold market data in two places, so using your main login here would fight with your own TWS or phone sessions. You'll need the US options market data (OPRA) and US equity subscriptions.
2. **Server software:** install Docker and `uv`, then clone this repo to `/opt/factor-rotation`.
3. **Settings:** copy `.env.example` to `.env` and fill it in. Give the instance an IAM role that can write to the bucket.
4. **Start the gateway:** `docker compose -f deploy/docker-compose.yml up -d`
5. **Install the schedules:**
   ```bash
   sudo cp deploy/systemd/* /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now fr-options-all.timer fr-options-etfs.timer fr-bars.timer
   ```
6. **First run:** `uv run fr bars` (backfills 5 years of bars), then `uv run fr options --symbols SPY,QQQ` to check permissions and timing.

Logs: `journalctl -u 'fr@*'`.
