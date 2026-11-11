"""Paper trading on IBKR: the ideas traded for real fills on a paper account, never a live one.

One book, the automatic book ("dynamic" in the files): the screen's candidates that a research pass takes. The
pass writes a verdict on each one (proposals.py); only the takes are traded, with the plan the pass wrote, inside
the outer limits in rules.py. The pass reviews open trades before the close and learns from every result
(learn.py). The selective book, which traded the recorded ideas as they stood, was retired on 2026-11-09.

Orders are market-on-open entries and market-on-close exits, so paper results line up with the
open-to-close scoring. The one exception closes a trade that only half filled:
- 9:15 AM ET  `fr paper open`   plans today's entries and sends them as market-on-open orders. It asks for
                                pre-market prices up to four times, and says so if it had to size on the last close.
- 9:40 AM ET  `fr paper check`  pulls the fills. A trade with no fills wasn't taken; a trade with one side
                                filled and the other dead has its filled side closed at the market.
- 3:45 PM ET  `fr paper close`  checks every open trade against its stop and time limit on current prices
                                and sends market-on-close exits. An earlier exit that never filled goes again.
- 4:20 PM ET  `fr paper sync`   pulls the day's fills, commissions and positions from the paper account.

Everything lands in journal/paper/, and each job's file lists its warnings in plain words; the dashboard's
Paper view and `fr paper report` show the latest. broker.py holds the guards that keep this off any live account.
FR_PAPER_SEND=1 normally comes from .env, which is read whenever the variable isn't already set: to run a
job by hand without sending, set FR_PAPER_SEND=0 on the command line.
"""
