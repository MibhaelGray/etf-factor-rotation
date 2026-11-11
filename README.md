# ETF Factor Rotation

Collects daily bars from Yahoo Finance and option chains from IBKR for the largest ETFs and their top constituents, turns them into positioning, volatility and technical signals, screens them each night for trade setups, and runs a paper book that takes only the candidates a research pass believes in, with every verdict and its reasoning kept and scored.

## Pieces

Data collection runs on the collector: a Mac that stays on, plugged in and logged in, with the IB Gateway running for option pulls. It can be a second Mac or the main one; the nightly job stashes any edits in progress around its `git pull` and commits only the files it writes. The main Mac runs the research passes and records their verdicts.

Bars and earnings dates come from Yahoo Finance through `yfinance`, with no login. Option chains, and the borrow figures that arrive with them, come from IBKR and are the only thing that needs the gateway; without it, those sections of the brief and dashboard are empty and everything else runs. Option data doesn't inform a trade decision. The IBKR connector, which doesn't need the gateway, supplies the night's market numbers, pre-market quotes and live marks. Web research feeds the research passes as context, not as stored data.

Yahoo is unofficial and can miss a split on its first day. `fr bars` repairs that for ETFs (QUAL and MTUM on 2026-11-05) and lists any other move of 40% or more in the last five sessions as `suspect`, so read its output.

| Command | Runs on | When (ET, trading days) | Writes |
|---|---|---|---|
| `fr options --group all` | collector | 10:00, 15:30 | `options_raw/`, `options_underlying/`, `exposures/`, `exposures_by_strike/` |
| `fr options --group etfs` | collector | 11:00–14:00 hourly | same, SPX and the ETFs only |
| `fr bars` | collector | 16:30 | `bars_daily/`, from Yahoo: the ETFs and stocks, the leveraged funds, AGG, and VIX, VIX3M and the 10-year yield |
| `deploy/run.sh nightly` | collector | 19:00 | runs `fr brief` and the rows below, commits what it and the day's passes wrote under `journal/`, pushes |
| `fr brief` | collector | in the nightly job | `journal/briefs/<as_of>.json`: regime, factor pairs, technicals, forced flows |
| `fr earnings` | collector | in the nightly job | `journal/reference/earnings.json`: earnings dates for every basket member and the largest stocks, from Yahoo Finance |
| `fr gaps` | collector | in the nightly job | `journal/reference/earnings_gaps.json`: what stocks did after earnings gaps, against SPY |
| `fr scan` | collector | in the nightly job | `journal/scans/<as_of>.json`: tonight's stock and basket candidates |
| `fr screen` | collector | in the nightly job | `journal/screens/<as_of>.json`: every stock, ETF, basket and factor pair checked against the setups in `journal.SETUPS`; the best 20 with what triggered each and a one-line chart verdict. Where a research pass looks |
| `fr paper open` | collector | 09:15 | `journal/paper/plans/`: today's paper entries, sent to the paper account as market-on-open orders |
| `fr paper check` | collector | 09:40 | `journal/paper/checks/`: entries that never filled, and half-filled trades closed at the market |
| `fr paper propose <draft>` | Mac | by 09:14 | `journal/paper/proposals/`: the research pass's verdict on each screen candidate for the next open; only takes are traded |
| `fr paper review <draft>` | Mac | by 15:44 | `journal/paper/reviews/`: hold, exit early or extend, for each open automatic trade |
| `fr paper close` | collector | 15:45 | `journal/paper/exits/`: trades leaving at the close (stop, time limit, broken pair), sent as market-on-close orders |
| `fr paper sync` | collector | 16:20 | `journal/paper/fills/`: the day's fills, commissions, positions and account value |
| `fr market` | Mac | after `git pull` | tonight's market rows for the dashboard: factor board, regime, dealer gamma, forced flows |
| `fr serve` | Mac | any time | serves the dashboard at http://localhost:8765 with a Refresh button that updates every panel in about 2.5 seconds: the paper account's fills, today's bar for every name from one bulk Yahoo quote (a five-day pull runs behind the first refresh each day to catch dividends and splits), the book's 15-minute P/L, and the brief and screen rebuilt from those bars for display only (a refresh never writes them to the journal; the research passes read the 7:00 PM ET ones). Earnings dates over 12 hours old update in the background, so they can't hold it up |
| `fr dashboard` | anywhere | any time | `dashboard/local.html`, a local copy of the dashboard built from the journal, and opens it |

The dashboard page lives in `dashboard/page.html`, the same file published to claude.ai as the [Factor Rotation Ledger](https://claude.ai/artifact/TVknCZmSXjToEPbmMGPSKv). The Refresh button only appears under `fr serve`; the claude.ai page can't run a pull. A refresh during the session stores a partial bar for the day, so the screen and the proposals' plan results wait for a session until its bars have been pulled after 4:15 PM ET. `fr dashboard` wraps it with `dashboard/shim.js` (a stand-in for the claude.ai runtime) and `dashboard/tokens.css`, and embeds tonight's market rows, so it opens from disk with no server. Tonight's market rows need the option data in `data/`, so on a machine without it that section shows as failed. Charts load d3 from a CDN.

Market data lives on the collector's disk under `FR_DATA_ROOT` (default `data/` in the repo). The journal (briefs, scans, screens and the paper book) travels through GitHub: the nightly job commits and pushes what it wrote and what the day's research passes recorded.

## Options data

Option chains are pulled for the S&P 500 index (SPX), the 46 ETFs and the 30 largest stocks (`universe.option_universe()`). SPX carries most of the index's dealer gamma (far more open interest than SPY), so it's pulled in every run, including the hourly ETF runs. Two of its classes are kept: SPXW (the dailies and weeklies, settled at the close) and SPX (the traditional monthlies, settled on the morning's opening prints, so their greeks run to 09:30). SPX can't be a trade's leg: an index can't be bought. For each underlying, the collector takes:
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

The leveraged funds and AGG are pulled as daily bars only; they aren't option underlyings or trade legs (`universe.idea_universe()`).

## The screen and the dashboard

At 7:00 PM ET the nightly job builds the brief, refreshes earnings dates and earnings-gap history, and runs the scan and the screen. The screen (`fr screen`) checks every stock, ETF, basket and factor pair against the setups in `journal.SETUPS` and keeps the best 20, each with what triggered it, default legs and a default plan. Every stock and ETF candidate also carries two chart measures: how tight its daily ranges were over the 10 sessions before the signal against the 50 before, and whether its trend is up, down or mixed (the 50-day average against the 200-day, and its direction over 20 sessions). They move the ranking only for breakouts: a tight base adds a little and a breakout inside a downtrend loses a little more (`TIGHT_BASE` and `ADJUST` in `screen.py`, with the five-year check behind them). The shortlist is built so its 20 slots hold different ideas: a name the paper book holds or closed in the last three sessions is left out (and listed under `in_book`), no setup takes more than five slots, funds that gave the same signal and move together count once, and each stock's default hedge is the market, sector or industry fund it has moved with most over 60 sessions (`hedge_fit`; `weak` when nothing moves with it). That shortlist is all the screen decides: the evening research pass judges each candidate, and only its takes are traded (see "The automatic book" below).

A trade's side is one symbol or a basket of stocks in equal weight. A pair is dollar-neutral; an outright long or short carries the market's move. A leg can be any US-listed stock or ETF, not only the standard universe: `fr bars` adds the paper book's legs and every basket member to its pull, and the nightly job runs `fr bars --pending` in case a proposal arrived after the 4:30 PM pull. Indexes such as SPX or VIX can't be legs.

`journal/reference/baskets.json` holds a watchlist of themed baskets, each with its hedge ETF, the story behind it, a reason for every member and a snapshot of the day's moves from IBKR. `fr bars` pulls daily bars for every member, `fr market` adds the baskets' spread against their hedge to the market rows, and the dashboard shows them on its Baskets page (`#baskets`). The watchlist isn't scored; a basket is scored only when a proposal trades it.

The dashboard's Chart page (`#chart=NVDA`) only works under `fr serve`, because the claude.ai page can't pull live data. Type a ticker, or click one in Baskets or Earnings, to see its last 60 days of 15-minute bars from Yahoo with daily and 15-minute averages, RSI, ATR and the nightly brief's technicals. It covers the names on the dashboard, pulls when opened, and saves nothing. Indicators on 15-minute bars use the regular session only. Yahoo reports no pre-market or after-hours volume, and its highs and lows there can be bad prints, so those candles show only the open and close. The chart loads Lightweight Charts from a CDN.

The dashboard's Earnings page (`#earnings`) lists who reports in the next ten sessions and how reports from the last five traded. Reactions come from our daily bars: the gap and the day on the first session that traded on the report, alone and against the name's basket hedge. Some Yahoo dates are estimates until the company confirms them.

The nightly and morning idea books (`fr record`, `fr check`, `fr morning`, `fr score`, the scorecard and the dashboard's Ideas and Track record views) were retired on 2026-11-09, along with the `factor-ideas` skill. Their files stay under `journal/ideas`, `checks`, `morning`, `scores`, `outcomes` and `intraday` as history; nothing reads them.

## Paper trading

The automatic book (called `dynamic` in the files) trades on an IBKR **paper** account, so its trades get real fills, slippage, borrow and commissions. It takes only what a research pass believes in: the pass judges every candidate on the screen, and its takes are traded, up to 5 a session at $10,000 a side, each with the plan the pass wrote (see "The automatic book" below). Caps are 60 open trades, $900,000 gross and $30,000 net in one symbol. Entries are market-on-open orders sent at 9:15 AM ET. At 9:40 AM ET the fills are checked: a trade with no fills wasn't taken, and a trade with one side filled and the other dead has its filled side closed with a plain market order, so half a pair doesn't sit unhedged all day. Exits are decided at 3:45 PM ET on Yahoo prices and sent as market-on-close orders; an earlier exit that never filled is sent again, unless the account's positions say it did fill.

Prices come from Yahoo, asked up to four times at the open and three at the close, because a Mac that has just woken for the job may have no network yet. The paper gateway can't stand in: it has no market data while the live session is logged in. If no pre-market price arrives, trades are sized on the last close. If no price arrives at 3:45 PM ET, that trade's stop isn't checked that day. Each of these, and any order the gateway cancels or comments on, is written as a warning in the job's file, shown on the dashboard's Paper view and in `fr paper report`, and raised as a Mac notification. The gateway keeps no reason for a cancelled order. The dashboard's Paper view shows every trade, the book's result and the difference between paper fills and the daily-bar model. Its profit and loss chart is built from the fills, after commissions, with open positions marked at each close. 1M, 3M and All use daily closes; under `fr serve`, 1D and 1W use 15-minute Yahoo bars, which are pulled when you open the view and not stored.

### The automatic book

The screen (`fr screen`) shortlists about 20 candidates a night. A research pass, run by Claude from the `automatic-book` skill, judges each one and records a verdict with `fr paper propose`: take or pass, a conviction from 1 to 5, and for a take the five steps of reasoning, what would prove it wrong, the legs and the plan. Code copies the screen's numbers into each record, fills in the mechanical legs and plan where the pass gave none, and records a candidate the pass never reached as "not reviewed", so every candidate has a record. The 9:15 AM job trades the takes, strongest conviction first, and takes nothing on a day with no proposals. The pass never sizes a trade or sends an order. The pass ranks what it judged and takes the best one to three that reach conviction 3; a night with no takes has to say why. An agent challenges each take, and a take is dropped only for a wrong fact, a thesis the price has already contradicted, or a pair that doesn't express the thesis. The pass can also add up to three ideas of its own from dated events the research turned up.

The pass runs three times a day:

| Pass | When (ET) | What it records |
|---|---|---|
| Evening | about 7:30 PM, after the screen | post-mortems for closed trades, the lessons file, and a verdict on every candidate for the next open |
| Pre-open | about 8:30 AM, in by 9:14 AM | each take stands or is stood down on the overnight session; verdicts on anything new |
| Pre-close | about 3:00 PM, in by 3:44 PM | hold, exit or extend for every open automatic trade (`fr paper review`) |

A plan is the pass's call inside outer limits in `paper/rules.py`: a time limit of 1 to 10 sessions and a stop between 1% and 8% of one side, checked at each close. A take needs conviction 3 or more, and one whose leg reports earnings inside the time limit must say why it holds through. At the pre-close review the pass can close a trade early for a stated reason, or ask for a trade on its last session to run 1 to 3 sessions longer, once; the 3:45 PM job grants that only if the trade is in profit. It can never widen a stop or keep a loser past its time limit: stops, time limits and broken pairs are enforced by code whatever the review says.

Learning runs on three records. Every proposal, taken or passed, is scored against its own plan on daily bars (first open to its stop or time limit, before costs), so the passes are the control for the takes. Each closed trade gets a post-mortem (`fr paper postmortem`): right for the reason given, right by luck, the reasoning broke, or noise, with a one-line lesson. And `journal/paper/lessons.md` holds the standing lessons the pass keeps. `fr paper learn` prints all of it on one page: results by verdict, conviction and setup, trades the review managed against their original plans, open trades with what would prove each wrong, and the lessons. Every pass reads it first, because the pass remembers nothing between runs. `fr paper hedges SYMBOL` lists each ETF's 60-session correlation and beta with a name, for choosing what goes on the other side. The dashboard's Paper view lists every verdict under "Research verdicts".

The three passes are scheduled tasks in the Claude desktop app (`automatic-book-evening-pass`, `-pre-open-pass`, `-pre-close-review`), set in this Mac's local time and run only while the app is open. Pause one from the app's Scheduled list; `fr paper halt` stops new entries whatever the passes record. Tests for the paper jobs and the research gate run against a stand-in broker: `uv run python -m unittest discover -s tests`.

`paper/broker.py` is the only code that can send an order. It refuses unless the port is a paper port (4002 or 7497, never the collector's), `FR_PAPER_ACCOUNT` is a DU account, and every account on the gateway is a paper account. Orders can only be market-on-open, market-on-close, or the plain market order the 9:40 AM check uses to close a half-filled trade. Without `FR_PAPER_SEND=1` every job is a dry run that prints its orders and writes nothing. `.env` supplies that setting whenever it isn't already set, so a job run by hand sends real paper orders unless you put `FR_PAPER_SEND=0` on the command line. `fr paper halt --reason "..."` stops new entries (exits still go out) until `fr paper resume`; `fr paper close --flatten` exits everything at today's close. `fr paper status` connects read-only and compares the account's positions with the books; `fr paper report` prints the results.

Setup, once:
1. In Client Portal, open the paper account (Settings → Account Settings → Paper Trading Account) and note its username and DU account number. It has its own login, so it never competes with the collector or your own live sessions.
2. Install Docker Desktop and turn on Settings → General → "Start Docker Desktop when you sign in".
3. Put the paper username and password in `.env.paper` (`TWS_USERID=` and `TWS_PASSWORD=`; git ignores the file), then start the paper gateway: `docker compose -f deploy/docker-compose.yml up -d ib-gateway-paper`. IBC logs it in, restarts it at 11:55 PM ET each night and logs back in, and Docker restarts it if it stops. `docker logs -f` with the container's name shows the login. It publishes the paper API on 127.0.0.1:4002 only, with the API's read-only switch off and the order precaution dialogs bypassed. The collector's live gateway is separate and stays read-only on 4001.
4. Add `FR_PAPER_ACCOUNT=DU...` to `.env` (`FR_PAPER_PORT=4002` is the default) and run `uv run fr paper status` to check the connection and the account.
5. The schedules (step 6 below) include `paper-open`, `paper-check`, `paper-close` and `paper-sync`; they skip quietly while `FR_PAPER_ACCOUNT` is unset. Jobs are dry runs, printing their orders to `~/Library/Logs/factor-rotation/paper-*.log`, until `FR_PAPER_SEND=1` is in `.env`. A job that starts after its auction cutoff (late from sleep, say) sends nothing.


## Research passes outside the Claude app (on trial)

`uv run fr pass pre-close --shadow` runs the swing book's pre-close review as a background job: Claude Code headless, signed in with `FR_CLAUDE_TOKEN` from `.env`, on the prompt in `deploy/passes/pre-close.md`. It can read files, search the web, spawn research agents and run only `fr trading-day`, `fr paper learn`, `fr quotes` and `fr paper review`; everything else is refused. It has no IBKR connector, so market numbers come from `uv run fr quotes SPY VIX ES=F` (Yahoo).

With `--shadow` it works on a copy of the journal in `data/shadow-journal`, so the pass scheduled in the Claude app stays the one that counts. The `pass-pre-close` launchd job runs it that way at 3:00 PM ET, and each run's result is in `data/pass-logs/`. Compare the two reviews for a few days before dropping `--shadow` in `deploy/run.sh` and pausing the app's task.

## Intraday book

A second book that trades the same ideas on a 15-minute clock, flat by 3:45 PM ET. Since 2026-11-12 Haiku's takes are traded on the paper account; passes and unjudged signals are only followed.

```bash
uv run fr intraday bars      # 15-minute bars from Yahoo for the liquid names, into data/bars_15m/
uv run fr intraday replay    # run the setups over those bars with no model; writes journal/intraday/replay.json
uv run fr intraday tick      # one live tick: move today's signals along, judge the new ones, send the takes' orders
uv run fr intraday report    # takes against passes over every rehearsal day
```

The `intraday-tick` launchd job runs the tick a minute after each 15-minute bar closes, 10:01 AM to 3:46 PM ET. Each new signal goes to Claude Haiku with the name's Yahoo headlines for a take or a pass (`intraday/model.py`), and takes and passes are both tracked to their exit by the same rules, in `journal/intraday/days/<date>.json`. The setups and exit rules are in `intraday/signals.py`. Results assume 8 bp of costs a trade.

Paper orders (`intraday/orders.py`): each take goes out at the tick that judges it as two market orders, the name and its hedge at $10,000 a side, on the swing book's paper account with the same guards. They fill about a minute after the signal bar closes, near the model's fill at the next bar's open. The tick whose check ends the trade (stop, target, time limit) sends the exit the same way, and the 3:46 PM tick closes whatever is still open. A pair with one leg dead is closed at the next tick, and an exit that didn't fill is sent again. Order refs start with the signal's id (`I-MMDD-SYM`), so the swing book's jobs ignore these fills, and its position check counts what the intraday book holds. The limits are 5 takes a day and 3 open at once. `journal/paper/HALT` stops new entries but not exits, `FR_PAPER_SEND=0` makes the orders a dry run, and the tick connects with its own client id (`FR_INTRADAY_CLIENT_ID`, 72) so it can overlap a swing book job. Each take's orders, fills and result after commissions are under `paper` in the day's file, beside its model result. If the Mac sleeps through the 3:46 PM tick, a position can stay open overnight, and the 4:20 PM sync reports it as a mismatch.

Haiku runs through Claude Code headless, which needs a long-lived login for unattended jobs: run `claude setup-token` once and put the token in `.env` as `FR_CLAUDE_TOKEN`. Without it every signal is recorded as undecided and only the bare rules are tracked.

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
