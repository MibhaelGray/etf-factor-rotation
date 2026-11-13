"""The intraday book: the automatic book's ideas on a 15-minute clock. A separate book with its own files.

  bars15.py    15-minute bars from Yahoo for the liquid names, kept in bars_15m/
  signals.py   the setups, as plain functions on one name's bars and its hedge's
  replay.py    the setups run over stored bars with no model, to see how often they fire and what they made
  live.py      the regular-session tick; orders.py its paper orders
  extended.py  after-hours, overnight and pre-market: news movers, judged and traded with limit orders

A tick started by deploy/run.sh is a scheduled one: it records its signals and trades its takes. A tick run by
hand only looks -- the setups run and the model still judges, but nothing is written and no order is sent --
unless `--execute` says otherwise.

Every trade is a pair, the name against its hedge fund in equal dollars, opened and closed inside one session.
The loop runs every 15 minutes, so a signal seen at one bar's close is filled at the next bar's open, and a stop
is seen at a close and filled at the next open too. The replay scores trades the same way.
"""
