# What this project is for

Written 2026-11-12 from a discussion with Michael. This is the reference for staying on track: new work should
serve what is written here. Change this file when the intent changes.

## The question

**How far can AI research be trusted?** Specifically: can AI research, working with the right technical
signals and data sources, produce alpha?

## Where it is going

An AI-run trading book that manages real money, reached in three stages:

1. **Paper, automatic (now).** The system decides and trades on an IBKR paper account by itself, to build a
   track record quickly.
2. **Real money, approved.** Michael approves every real trade. The real account would be about $15,000.
3. **Real money, self-managing.** The system manages the book almost entirely itself.

Along the way it is also three things at once: a research assistant that surfaces good trades, an experiment
whose answer matters whichever way it comes out, and something to build and learn from.

## What "it works" means

- **Consistently beating the S&P 500's return without taking on excessive risk**: an above-average Sharpe ratio.
- **The S&P 500 is the benchmark** however the book chooses to trade (pairs, outright longs, outright shorts).
- **Real signal means statistically significant**, not a good-looking week. The measure:
  - *Main test:* the trades the research takes beat the ones it passes on, with under a 5% chance the gap is luck.
  - *Second test:* the book's return after costs is reliably above zero and above the S&P's over the same days.
  - *Early test (swing book, added 2026-11-13):* the evening pass ranks every candidate it judges, best to worst,
    and the better-ranked ones go on to do better. It uses all twenty or so verdicts a night, not only the takes,
    so it can speak in about 20 nights. It is an early read on the research's judgment, not a substitute for the
    main test.
  - The test and the bar are fixed in advance and checked on a schedule (weekly), not whenever it looks good.
- **Excessive risk: a 10% drop.** If a paper book falls 10%, trading pauses and we work out what to do differently.
  The drop is measured against the book's full capital: $90,000 on the swing book's $900,000, $10,000 on the
  intraday book's $100,000, counted from the book's high. The pause is automatic (`drawdown.py`): that book opens
  no new trades, its exits still go out, and it stays paused until resumed by hand with
  `fr paper resume --book <book>`.

## How it is built

- **Technical signals find the candidates; AI research decides.** Both books work this way.
- **Swing book** (held 1–10 sessions): the closest to how Michael trades. A nightly screen from prices and
  volume shortlists setups; scheduled research passes judge them, add their own ideas from coming events, and
  take the best.
- **Intraday book** (flat by the close, plus after-hours and overnight): signals from 15-minute bars, judged by
  a small model (Haiku) that researches each one. An experiment alongside the swing book: the aim is to find
  out whether both styles can succeed.
- **Every verdict is recorded before the trade and scored afterwards**, taken or passed, so the passes are the
  control group. The swing book's evening pass also ranks the night's verdicts, and the ranking is scored.
- **Paper sizes stay large** ($10,000 a side). What matters is percent return, and a bigger book gives more
  trades and a faster answer. Sizing for a $15,000 account is a later step, before real money.

## Data

Today: prices, volume, earnings dates, and web research the AI reads itself during each pass (there is no news
sentiment score). If there is no real signal in the next few weeks, we start testing more data sources.
Short interest is the first to add; the rest to be decided then.

## Working rules already set

- Paper only until stage 2. Nothing places a live order.
- Scheduled passes trade and log. A pass run by hand records nothing and sends nothing unless told to.
- Reasoning, plan and "wrong if" are written before the trade.
- Take trades to learn: a night with no takes shows nothing.
- Option data doesn't inform a trade decision.
- Orders fill at once: buy at the ask, sell at the bid, unless the spread is too wide (2% swing, 1% intraday).
- A trade is in the book from its first order, and the books add up to the account.

## Not decided yet

- What has to be true to move from stage 1 to 2, and from 2 to 3.
- What we do if the test fails after a fair run.
- Whether the research passes should lean toward outright trades now that the S&P is the benchmark: the screen
  gives every candidate a hedge by default, and a hedged book can do well and still trail a rising S&P.
- Which data sources to test after short interest.
