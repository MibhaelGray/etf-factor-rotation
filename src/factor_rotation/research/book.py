"""The systematic book: tonight's target spread weights from the incumbent
signal blend, recorded write-once next to the discretionary ideas and scored
over the same session (open to close) so the two can be compared directly.
"""

import json
from datetime import date, datetime

from ..config import NY
from ..journal import JOURNAL, _bar, _json_default, _write_once, next_trading_day
from ..ib.bars import load
from .backtest import combo_score, prepare, run
from .signals import SIGNALS

INCUMBENT = {"name": "combo_equal", "members": sorted(SIGNALS), "horizon": 20, "band": 1.0}


def build(as_of: date | None = None) -> dict:
    research = prepare()
    score = combo_score(research, INCUMBENT["members"])
    result = run(research, INCUMBENT["name"], score, INCUMBENT["horizon"], band=INCUMBENT["band"])
    last = result.positions.index[-1] if as_of is None else result.positions.loc[:str(as_of)].index[-1]
    weights = result.positions.loc[last]
    weights = weights[weights.abs() > 1e-6].round(4)
    legs = {s.name: (s.long, s.short) for s in research.spreads}
    return {
        "as_of": str(last.date()),
        "trade_date": next_trading_day(last.date()).isoformat(),
        "built_at": datetime.now(NY).isoformat(),
        "incumbent": INCUMBENT,
        "gross_exposure": float(weights.abs().sum()),
        "positions": [{"spread": k, "long": legs[k][0], "short": legs[k][1], "weight": float(v)} for k, v in weights.sort_values().items()],
    }


def record(as_of: date | None = None):
    book = build(as_of)
    return _write_once(JOURNAL / "book" / f"{book['trade_date']}.json", book)


def score() -> list:
    written = []
    for path in sorted((JOURNAL / "book").glob("*.json")):
        out = JOURNAL / "book_scores" / path.name
        if out.exists():
            continue
        book = json.loads(path.read_text())
        day = date.fromisoformat(book["trade_date"])
        symbols = sorted({p[k] for p in book["positions"] for k in ("long", "short")} | {"SPY"})
        bars = load(symbols)
        if bars.empty or _bar(bars, "SPY", day) is None:
            continue
        pnl, missing = 0.0, []
        for p in book["positions"]:
            lb, sb = _bar(bars, p["long"], day), _bar(bars, p["short"], day)
            if lb is None or sb is None:
                missing.append(p["spread"])
                continue
            pnl += p["weight"] * ((lb["close"] / lb["open"] - 1) - (sb["close"] / sb["open"] - 1))
        spy = _bar(bars, "SPY", day)
        written.append(_write_once(out, {
            "trade_date": day.isoformat(),
            "scored_at": datetime.now(NY).isoformat(),
            "open_to_close_bps": round(pnl * 1e4, 1),
            "spy_open_to_close_bps": round((spy["close"] / spy["open"] - 1) * 1e4, 1),
            "missing": missing,
        }))
    return written


def describe(book: dict) -> str:
    rows = [f"{p['weight']:+.3f}  {p['spread']:<20} long {p['long']:<5} short {p['short']}" for p in book["positions"]]
    return json.dumps({k: book[k] for k in ("as_of", "trade_date", "gross_exposure")}, default=_json_default) + "\n" + "\n".join(rows)
