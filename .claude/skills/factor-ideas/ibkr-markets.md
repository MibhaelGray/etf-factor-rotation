# Market numbers from the IBKR connector

Take rates, volatility, oil, the dollar and index futures from the IBKR connector (`get_price_snapshot` with `last`, `prior_close`, `change`), not from articles. Pass `exchange` for every index and future, or the snapshot comes back empty. These quotes are delayed about 15 minutes on this account, so say "as of" with the time when it matters (the pre-open check).

## Indexes (contract ids don't change)

| What | Symbol | contract_id | exchange | Note |
|---|---|---|---|---|
| 10-year yield | TNX | 416921 | CBOE | yield × 10: 49.58 is 4.958% |
| 5-year yield | FVX | 416730 | CBOE | yield × 10 |
| VIX | VIX | 13455763 | CBOE | |
| 1-day VIX | VIX1D | 627990891 | CBOE | |
| 9-day VIX | VIX9D | 322592334 | CBOE | |
| 3-month VIX | VIX3M | 47511905 | CBOE | VIX / VIX3M above 1 is inverted |
| 6-month VIX | VIX6M | 322592386 | CBOE | |

## Futures (roll: resolve the front month each time)

Call `search_futures` with the underlying id, sort by `contract_month`, and take the earliest that isn't within a few days of `last_trading_date`. Front months as of 2026-11-03 are listed so you can start from them.

| What | Root | underlying id | exchange | Front on 2026-11-03 |
|---|---|---|---|---|
| S&P 500 e-mini | ES | 11004968 | CME | ESZ6 515416632 |
| Nasdaq 100 e-mini | NQ | 11004958 | CME | NQZ6 563947726 |
| Russell 2000 e-mini | RTY | 24771018 | CME | RTYZ6 780341989 |
| WTI crude | CL | 17340715 | NYMEX | CLZ6 296574787 |
| Brent crude | BZ | 45631501 | NYMEX | BZF7 339981246 |
| Gold | GC | 17340718 | COMEX | resolve |
| US dollar index | DX | 49315275 | NYBOT | DXZ6 839021595 |
| 2-year note (price) | ZT | 11160388 | CBOT | ZTZ6 869928219 |
| VIX futures | VX | 13455763 | CFE | VXX6 857096518, VXZ6 866999753 |

Futures trade nearly around the clock, so before the open their `change` is the overnight move from the prior settlement.

## ETFs and pre-market

ETF legs are SMART-routed stocks (no `exchange` needed). Before 09:30, `last` is the latest pre-market trade and `prior_close` is the previous session's close. For the pre-market path, `get_price_history` with `step: FIFTEEN_MINS`, `period: ONE_DAY`, `outside_rth: true` returns bars from 04:00 ET.
