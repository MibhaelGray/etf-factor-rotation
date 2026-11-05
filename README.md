# ETF Factor Rotation

Collects daily bars from Yahoo Finance and option chains from IBKR for the largest ETFs and their top constituents, turns them into positioning, volatility and technical signals, and keeps a write-once journal of nightly factor pair ideas with a running scorecard.

## Pieces

Data collection runs on the collector: a Mac that stays on, plugged in and logged in, with the IB Gateway running for option pulls. It can be a second Mac or the main one; the nightly job stashes any edits in progress around its `git pull` and commits only the files it writes. The main Mac reads the brief, runs the research and records the ideas.

Bars and earnings dates come from Yahoo Finance through `yfinance`, with no login. Option chains, and the borrow figures that arrive with them, come from IBKR and are the only thing that needs the gateway; without it, those sections of the brief and dashboard are empty and everything else runs. Option data doesn't inform the ideas. The IBKR connector, which doesn't need the gateway, supplies the night's market numbers, pre-market quotes and live marks. Web research feeds the nightly dossier as context, not as stored data.

Yahoo is unofficial and can miss a split on its first day. `fr bars` repairs that for ETFs (QUAL and MTUM on 2026-11-05) and lists any other move of 40% or more in the last five sessions as `suspect`, so read its output.

| Command | Runs on | When (ET, trading days) | Writes |
|---|---|---|---|
| `fr options --group all` | collector | 10:00, 15:30 | `options_raw/`, `options_underlying/`, `exposures/`, `exposures_by_strike/` |
| `fr options --group etfs` | collector | 11:00–14:00 hourly | same, SPX and the ETFs only |
| `fr bars` | collector | 16:30 | `bars_daily/`, from Yahoo: the ETFs and stocks, the leveraged funds, AGG, and VIX, VIX3M and the 10-year yield |
| `deploy/run.sh nightly` | collector | 19:00 | runs `fr intraday`, `fr score` and `fr brief`, commits `journal/`, pushes |
| `fr intraday` | collector | in the nightly job | `journal/intraday/`, from Yahoo: the session's 15-minute bars (04:00–16:00) for the idea legs and SPY. Thin ETFs can have gaps before the open |
| `fr earnings` | collector | in the nightly job | `journal/reference/earnings.json`: earnings dates for every basket member and the largest stocks, from Yahoo Finance |
| `fr record` | Mac | ~19:30 | `journal/ideas/`, committed and pushed |
| `fr check` | Mac | 08:30–09:25 | `journal/checks/`: the pre-open stands / stand-down decision per idea; refused after the open |
| `fr morning` | Mac | after the check, before 09:30 | `journal/morning/ideas/`: the morning book, 1–2 new ideas from the overnight session, scored apart from the nightly book; refused after the open |
| `fr scorecard` | anywhere | any time | prints the scorecard |
| `fr ledger` | Mac | after `git pull` | every idea and its result as JSON rows for the [Factor Rotation Ledger](https://claude.ai/artifact/TVknCZmSXjToEPbmMGPSKv) dashboard |
| `fr market` | Mac | after `git pull` | tonight's market rows for the dashboard: factor board, regime, dealer gamma, forced flows |
| `fr dashboard` | anywhere | any time | `dashboard/local.html`, a local copy of the dashboard built from the journal, and opens it |

The dashboard page lives in `dashboard/page.html`, the same file published to claude.ai. `fr dashboard` wraps it with `dashboard/shim.js` (a stand-in for the claude.ai runtime) and `dashboard/tokens.css`, and embeds the ledger and tonight's market rows, so it opens from disk with no server. Tonight's market rows need the option data in `data/`, so on a machine without it that section shows as failed. Charts load d3 from a CDN.

Market data lives on the collector's disk under `FR_DATA_ROOT` (default `data/` in the repo). The journal (briefs, ideas, scores) travels through GitHub: the collector pushes scores and briefs, and the main Mac pulls them, records the ideas and pushes those back.

## Options data

Option chains are pulled for the S&P 500 index (SPX), the 46 ETFs and the 30 largest stocks (`universe.option_universe()`). SPX carries most of the index's dealer gamma (far more open interest than SPY), so it's pulled in every run, including the hourly ETF runs. Two of its classes are kept: SPXW (the dailies and weeklies, settled at the close) and SPX (the traditional monthlies, settled on the morning's opening prints, so their greeks run to 09:30). SPX isn't an idea leg: an index can't be bought. For each underlying, the collector takes:
- the 5 nearest expiries within 8 days, where most dealer gamma sits
- the next 3 monthly expiries, out to 120 days for ETFs or 75 days for stocks

Strikes are kept within ±2 standard deviations of spot, using the underlying's IV, with a floor of 3% and a cap of 10% for ETFs and SPX or 15% for stocks. Within 3% of spot every strike is kept; beyond that only round strikes (every $5 on SPY, every 50 points on SPX). That keeps SPY near 850 contracts, SPX near 2,200 (SPXW's 8 expiries plus SPX's 3 monthlies) and a full run near 17,000, while the strikes and expiries that drive gamma, walls and skew stay in. Contract ids are cached per underlying and trading class until expiry, along with the built strikes IBKR doesn't list, so each strike is looked up once. Each contract stores:
- bid, ask and sizes
- last price, volume and open interest
- IBKR's model greeks

`fr options` then computes our own greeks for every contract. It implies a forward from put-call parity near the money, solves IV from the quote mid on the out-of-the-money side of each strike, and uses those to compute delta, gamma, vega, theta, vanna and charm. From those it computes, per underlying:
- **Positioning:** total and 0DTE gamma exposure (GEX), the gamma flip level, call and put walls, vanna and charm exposure
- **Volatility surface:** ATM IV at 30 and 90 days, term slope, 25-delta skew
- **Flow:** put/call ratios by volume and open interest, premium traded, 0DTE share, and unusual volume (volume above open interest)

GEX assumes dealers are long calls and short puts. Open interest updates once a day, overnight.

## Forced flows

The brief's `forced_flows` section estimates trading that has to happen regardless of anyone's view, from the same stored data:
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
   "stand_down_if": ["MTUM gaps more than 1 ATR above USMV pre-market", "..."],
   "drivers": ["stretched_pair", "positioning"]}]}
```

Each idea is entered at the next open and exited at the close; close-to-close is recorded alongside. A pair is dollar-neutral; an outright `long` or `short` (leave the other side out) carries the market's move. A leg can be any US-listed stock or ETF, not only the standard universe: names outside it are checked with the IBKR connector before recording (a US listing and about $20M a day of volume) and have their bars pulled before scoring, as basket members do. Indexes such as SPX or VIX can't be legs.

Either side can instead be a themed basket of 3–10 stocks in equal weight, named by `long_label` or `short_label`:

```json
{"long": ["VST", "TLN", "NRG", "CEG"], "long_label": "Texas power", "short": "XLU", "factor": "power_policy", "...": "..."}
```

Single legs and basket members can come from outside the standard universe. `fr bars` adds the legs of every unscored idea to its pull, and the nightly job runs `fr bars --pending` before scoring in case the ideas arrived after the 16:30 pull. If a leg's bars are still missing, scoring waits up to five days before recording the idea as missing.

`journal/reference/baskets.json` holds a watchlist of themed baskets, each with its hedge ETF, the story behind it, a reason for every member and a snapshot of the day's moves from IBKR. `fr bars` pulls daily bars for every member, `fr market` adds the baskets' spread against their hedge to the market rows, and the dashboard shows them on its Baskets page (`#baskets`). The watchlist isn't scored; only an idea that uses a basket is.

The dashboard's Earnings page (`#earnings`) lists who reports in the next ten sessions and how reports from the last five traded. Reactions come from our daily bars: the gap and the day on the first session that traded on the report, alone and against the name's basket hedge. Some Yahoo dates are estimates until the company confirms them.

Before the open, `fr check` records whether each idea stands or is stood down under its own `stand_down_if` rules. The ideas never change; the scorecard reports the full book and the book after the check side by side.

After the check, `fr morning` can record 1–2 new ideas for the session from what happened overnight, before the open. They are the morning book: the same fields as a nightly idea, with `overnight` (what the idea reacts to) in place of `stand_down_if`, ids like `2026-11-05-m1`, and scores in `journal/morning/scores/`. The scorecard and dashboard keep the two books apart, so the record shows whether reacting to the overnight session helps. Push the morning file before 19:00 so the collector's intraday pull and scoring include its legs.

## Collector setup (macOS)

1. **IBKR login:** the account must be on IBKR Pro (Lite accounts can't use the API). Create a second username in Client Portal (Settings → Users & Access Rights) and subscribe it to US options (OPRA) and US equity market data. Its own subscriptions mean your own TWS or phone sessions never cut off the collector. Expect an IB Key approval on your phone about once a week, when the gateway's session expires.
2. **Software:** install Docker Desktop (Settings → General → Start Docker Desktop when you sign in), `uv` and git. Clone this repo, set `git config user.name` and `user.email` in it, and give the Mac an SSH key with write access to the GitHub repo and no passphrase, so the nightly job can push unattended.
3. **Settings:** copy `.env.example` to `.env` and fill in the login.
4. **Start the gateway:** `docker compose -f deploy/docker-compose.yml up -d`. It restarts with Docker Desktop.
5. **Keep it awake on power.** Jobs that come due while the Mac sleeps run late, when it wakes, so the snapshots would be mistimed. In System Settings, turn on automatic login, and under Battery → Options turn on "Prevent automatic sleeping on power adapter when the display is off". Or from a terminal:
   ```bash
   sudo pmset -c sleep 0 disksleep 0
   ```
   Leave the lid open, or closed with an external display attached; a closed lid without one sleeps regardless. Each job also runs under `caffeinate`, so the Mac stays awake until it finishes.
6. **Install the schedules:** `python3 deploy/launchd/install.py`. It converts the New York schedule to the Mac's time zone (rerun it if that changes) and installs one launchd agent per job. `--dry-run` shows the plists without installing, `--uninstall` removes them.
7. **First run:** `uv run fr bars` (backfills 5 years of bars, no gateway needed), then `uv run fr options --symbols SPY,QQQ` to check permissions and timing.

Logs: `~/Library/Logs/factor-rotation/<job>.log`. To run a job now: `launchctl kickstart gui/$(id -u)/com.factor-rotation.nightly`.

A Linux box works too: `deploy/systemd/` holds the same schedule as systemd timers, with the repo at `/opt/factor-rotation`.

## Moving the collector to another Mac

1. Set up the new Mac with steps 1–6 above, but don't start the gateway yet.
2. On the old one: `python3 deploy/launchd/install.py --uninstall`, then `docker compose -f deploy/docker-compose.yml down`. One gateway per username, so stop this one first.
3. Copy the market data across (Remote Login on, on the new Mac): `rsync -az data/ newmac.local:<repo path>/data/`. Past option chains can't be re-downloaded, so check the file counts match before deleting anything.
4. On the new Mac: start the gateway, install the schedules and kickstart `bars` to check it connects.

## Backup

The collector's disk holds the only copy of the market data, and past option chains can't be re-downloaded. Copy it to the main Mac now and then (turn on System Settings → General → Sharing → Remote Login on the collector first):

```bash
rsync -az collector.local:<repo path>/data/ ~/Documents/"ETF Factor Rotation"/data/
```
