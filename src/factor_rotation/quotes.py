"""Current prices from Yahoo for the research passes: `fr quotes SPY NVDA VIX ES=F`.

A pass that runs outside the Claude app has no IBKR connector, so this is where it reads the market. Stocks and
ETFs go by their symbols, the indexes by the names used elsewhere in the project (VIX, VIX3M, VIX9D, TNX, FVX),
and futures by Yahoo's own tickers: ES=F, NQ=F, RTY=F (index futures), CL=F, BZ=F (crude), HO=F, RB=F, GC=F,
DX-Y.NYB (dollar index), ZT=F. Yahoo's quotes can run up to 15 minutes behind, so each line carries its time.
"""

from datetime import datetime

from . import bars
from .config import NY

INDEXES = {"VIX": "^VIX", "VIX3M": "^VIX3M", "VIX9D": "^VIX9D", "VIX1D": "^VIX1D", "VIX6M": "^VIX6M", "TNX": "^TNX", "FVX": "^FVX"}
FIELDS = ("regularMarketPrice,regularMarketPreviousClose,regularMarketChangePercent,regularMarketTime,regularMarketOpen,"
          "regularMarketDayHigh,regularMarketDayLow,regularMarketVolume,preMarketPrice,preMarketChangePercent,preMarketTime,marketState")


def fetch(names: list[str]) -> list[dict]:
    tickers = {INDEXES.get(n.upper(), n.upper().replace(" ", "-")): n.upper() for n in names}
    got = bars._quotes(list(tickers), FIELDS)
    out = []
    for t, name in tickers.items():
        q = got.get(t)
        if not q or q.get("regularMarketPrice") is None:
            out.append({"name": name, "error": "no quote"})
            continue
        row = {"name": name, "last": q["regularMarketPrice"], "prior_close": q.get("regularMarketPreviousClose"),
               "change_pct": q.get("regularMarketChangePercent"), "open": q.get("regularMarketOpen"),
               "high": q.get("regularMarketDayHigh"), "low": q.get("regularMarketDayLow"), "volume": q.get("regularMarketVolume"),
               "as_of": datetime.fromtimestamp(q["regularMarketTime"], NY).strftime("%b %-d, %-I:%M %p ET") if q.get("regularMarketTime") else None,
               "state": q.get("marketState")}
        if q.get("marketState") == "PRE" and q.get("preMarketPrice"):
            row.update(pre_market=q["preMarketPrice"], pre_market_change_pct=q.get("preMarketChangePercent"),
                       pre_market_as_of=datetime.fromtimestamp(q["preMarketTime"], NY).strftime("%-I:%M %p ET") if q.get("preMarketTime") else None)
        out.append(row)
    return out


def text(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        if "error" in r:
            lines.append(f"{r['name']:<9} {r['error']}")
            continue
        line = f"{r['name']:<9} last {r['last']:>10,.2f}  {r['change_pct'] or 0:+6.2f}% on prior close {r['prior_close'] or float('nan'):,.2f}  as of {r['as_of']}"
        if "pre_market" in r:
            line += f"  | pre-market {r['pre_market']:,.2f} ({r['pre_market_change_pct'] or 0:+.2f}%) at {r['pre_market_as_of']}"
        lines.append(line)
    return "\n".join(lines)
