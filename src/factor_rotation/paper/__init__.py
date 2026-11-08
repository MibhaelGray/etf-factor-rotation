"""Paper trading on IBKR: the ideas traded for real fills on a paper account, never a live one.

Two books, each trade tagged with its book:
- selective: the recorded ideas (nightly and morning books), taken as recorded, minus any the pre-open check
  stood down.
- dynamic: the screen's top candidates, taken mechanically with a default plan per setup (rules.py).

Orders are only market-on-open entries and market-on-close exits, so paper results line up with the
open-to-close scoring:
- 9:22 AM ET  `fr paper open`   plans today's entries and sends them as market-on-open orders.
- 3:45 PM ET  `fr paper close`  checks every open trade against its stop and time limit on current prices
                                and sends market-on-close exits.
- 4:20 PM ET  `fr paper sync`   pulls the day's fills, commissions and positions from the paper account.

Everything lands in journal/paper/. broker.py holds the guards that keep this off any live account.
"""
