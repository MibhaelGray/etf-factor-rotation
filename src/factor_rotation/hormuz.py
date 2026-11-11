"""Ship traffic through the Strait of Hormuz and the futures that price it, for the dashboard's Hormuz view.

The live layer is hormuz.data-tracking.net (CC BY 4.0, free, no key): ship crossings from AIS, polled every 30 minutes,
with the last 30 days kept. It counts many more ships than PortWatch, because the two define a transit differently,
so each is shown as its own series.

Transits come from IMF PortWatch (satellite ship tracking, daily, published about weekly with a few days' lag). It
counts tankers and their cargo capacity, not barrels, so oil and LNG flows are scaled from the U.S. EIA's 2024
figures by tanker capacity against the 2025 average. Prices come from Yahoo. Nothing here informs a trade.
"""

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from . import bars
from .config import NY, settings

log = logging.getLogger(__name__)

PORTWATCH = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/Daily_Chokepoints_Data/FeatureServer/0/query"
SINCE = "2025-01-01"
BASE_YEAR = 2025
LIVE = "https://hormuz.data-tracking.net/api"
STALE = {"transits": timedelta(hours=6), "history": timedelta(hours=6), "live": timedelta(minutes=15)}
# The live tracker's ship categories that carry oil, and those that carry gas.
OIL_SHIPS = {"Crude Oil Tanker", "Tanker", "VLCC/ULCC", "Oil Products Tanker", "Oil/Chemical Tanker", "Chemical Tanker", "Asphalt/Bitumen Tanker"}
GAS_SHIPS = {"LNG Tanker", "LPG Tanker", "LPG/Chemical Tanker"}
# EIA, "World Oil Transit Chokepoints": 2024 flows through the strait.
EIA = {"oil_mbd": 20.0, "lng_bcfd": 10.5, "oil_share": "about a fifth of world oil use", "lng_share": "about a fifth of world LNG trade"}
# What prices the strait: Gulf crude sells off Dubai, and Qatar's LNG mostly goes to Asia on JKM.
CONTRACTS = [
    {"ticker": "DCB=F", "name": "Dubai crude", "unit": "$/bbl", "kind": "oil", "role": "The benchmark Gulf crude is sold on"},
    {"ticker": "BZ=F", "name": "Brent crude", "unit": "$/bbl", "kind": "oil", "role": "The world benchmark"},
    {"ticker": "JKM=F", "name": "JKM LNG", "unit": "$/MMBtu", "kind": "gas", "role": "Asian LNG, where most Qatari cargoes go"},
    {"ticker": "TTF=F", "name": "TTF gas", "unit": "EUR/MWh", "kind": "gas", "role": "European gas, which bids for the same cargoes"},
]
QUOTE_FIELDS = "regularMarketPrice,regularMarketPreviousClose,regularMarketTime"


def _file(name: str) -> Path:
    return Path(settings().data_root) / "hormuz" / f"{name}.json"


def _load(name: str) -> dict | None:
    path = _file(name)
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except ValueError:
        return None


def _save(name: str, payload: dict) -> None:
    path = _file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    tmp.replace(path)


def _get(url: str, params: dict, tries: int = 4) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(1, tries + 1):
        try:
            request = urllib.request.Request(full, headers={"User-Agent": "factor-rotation-dashboard"})
            with urllib.request.urlopen(request, timeout=20) as r:
                return json.load(r)
        except OSError:
            if attempt == tries:
                raise
            time.sleep(3)


def pull_transits() -> int:
    body = _get(PORTWATCH, {"where": f"portname='Strait of Hormuz' AND date >= DATE '{SINCE}'", "orderByFields": "date ASC",
                            "outFields": "date,n_tanker,n_total,capacity_tanker,capacity", "resultRecordCount": 4000, "f": "json"})
    days = [[str(a["date"])[:10], a["n_tanker"], a["n_total"], a["capacity_tanker"], a["capacity"]]
            for a in (f["attributes"] for f in body["features"])]
    if len(days) < 300:
        raise ValueError(f"PortWatch returned {len(days)} days")
    _save("transits", {"pulled_at": datetime.now(NY).isoformat(), "days": days})
    return len(days)


def pull_history() -> int:
    import yfinance as yf

    tickers = [c["ticker"] for c in CONTRACTS]
    with bars._yahoo:
        data = yf.download(tickers, period="1y", interval="1d", auto_adjust=False, group_by="ticker", threads=True,
                           timeout=10, progress=False)
    series = {}
    for t in tickers:
        close = data[t]["Close"].dropna() if t in data.columns.get_level_values(0) else pd.Series(dtype=float)
        if len(close) > 20:
            series[t] = [[d.date().isoformat(), round(float(v), 3)] for d, v in close.items()]
    if not series:
        raise ValueError("Yahoo returned no futures history")
    _save("history", {"pulled_at": datetime.now(NY).isoformat(), "series": series})
    return len(series)


def pull_quotes() -> int:
    """The futures' latest prices from one bulk quote, on every refresh. Keeps the last ones if Yahoo doesn't answer."""
    quotes = bars._quotes([c["ticker"] for c in CONTRACTS], QUOTE_FIELDS)
    out = {t: {"last": q.get("regularMarketPrice"), "prev": q.get("regularMarketPreviousClose"), "time": q.get("regularMarketTime")}
           for t, q in quotes.items() if q.get("regularMarketPrice")}
    if out:
        _save("quotes", {"pulled_at": datetime.now(NY).isoformat(), "quotes": out})
    return len(out)


def _kind(category: str) -> str:
    return "oil" if category in OIL_SHIPS else "gas" if category in GAS_SHIPS else "other"


def pull_live() -> int:
    """The live tracker: daily crossings by ship type for 30 days, the last 24 hours and its oil export estimate."""
    days: dict[str, dict] = {}
    for r in _get(f"{LIVE}/crossings/daily/by_cat", {"days": 30}):
        if r["direction"] not in ("inbound", "outbound"):
            continue  # "in_strait" is ships inside the zone, not crossings
        d = days.setdefault(r["day"], {"day": r["day"], "in": 0, "out": 0, "oil": 0, "gas": 0, "other": 0})
        d["in" if r["direction"] == "inbound" else "out"] += r["count"]
        d[_kind(r["ship_category"])] += r["count"]
    last24 = {"oil": 0, "gas": 0, "other": 0}
    for r in _get(f"{LIVE}/crossings/by_type", {"hours": 24}):
        if r["direction"] in ("inbound", "outbound"):
            last24[_kind(r["ship_category"])] += r["count"]
    summary = _get(f"{LIVE}/summary", {})
    oil = _get(f"{LIVE}/oil_export/daily", {"days": 30})
    if not days or "last_poll" not in summary:
        raise ValueError("the live tracker returned nothing")
    _save("live", {"pulled_at": datetime.now(NY).isoformat(), "last_poll": summary["last_poll"],
                   "last24": {**last24, "in": summary.get("inbound"), "out": summary.get("outbound"), "crossings": summary.get("total_crossings"),
                              "in_strait": summary.get("in_strait"), "oil_bbl": summary.get("oil_export_barrels")},
                   "days": sorted(days.values(), key=lambda d: d["day"]),
                   "oil_export": [{"day": d["day"], "bbl": d["total_barrels"]} for d in oil.get("days", [])],
                   "oil_avg_bbl": oil.get("avg_bbl_per_day")})
    return len(days)


def _stale(name: str) -> bool:
    d = _load(name)
    return d is None or datetime.now(NY) - datetime.fromisoformat(d["pulled_at"]) > STALE[name]


def stale() -> bool:
    return any(_stale(n) for n in STALE)


def pull() -> None:
    """Whatever is older than its limit. Each is kept from the last pull if its source fails."""
    for name, fn in (("transits", pull_transits), ("history", pull_history), ("live", pull_live)):
        if not _stale(name):
            continue
        try:
            fn()
        except Exception as e:
            log.warning("hormuz_pull_failed what=%s error=%r", name, e)


def _mean(rows: list[list], col: int) -> float | None:
    return sum(r[col] for r in rows) / len(rows) if rows else None


def summary(days: list[list]) -> dict:
    """The last 7 reported days against the base year's daily average, and the flows that implies."""
    base = [d for d in days if d[0].startswith(str(BASE_YEAR))]
    last = days[-7:]
    base_cap, cap = _mean(base, 3), _mean(last, 3)
    share = cap / base_cap if base_cap and cap is not None else None
    closed = next((d[0] for i, d in enumerate(days) if i >= 7 and base_cap and _mean(days[i - 6:i + 1], 3) < 0.25 * base_cap
                   and d[0] > f"{BASE_YEAR}-12-31"), None)
    return {"base_year": BASE_YEAR, "base_tankers": _mean(base, 1), "base_ships": _mean(base, 2), "base_capacity": base_cap,
            "tankers_7d": _mean(last, 1), "ships_7d": _mean(last, 2), "capacity_7d": cap, "share": share,
            "oil_mbd": None if share is None else round(EIA["oil_mbd"] * share, 2),
            "lng_bcfd": None if share is None else round(EIA["lng_bcfd"] * share, 2), "below_quarter_since": closed}


def rows() -> list[dict]:
    """One `hormuz` row for the dashboard, or nothing before the first pull."""
    transits, history, quotes = _load("transits"), _load("history") or {"series": {}}, _load("quotes") or {"quotes": {}}
    if not transits:
        return []
    days = transits["days"]
    contracts = []
    for c in CONTRACTS:
        series = history["series"].get(c["ticker"], [])
        q = quotes["quotes"].get(c["ticker"]) or {}
        last = q.get("last") or (series[-1][1] if series else None)
        prev = q.get("prev") or (series[-2][1] if len(series) > 1 else None)
        if last is None:
            continue
        at = datetime.fromtimestamp(q["time"], NY).isoformat() if q.get("time") else None
        start = next((v for d, v in series if d >= f"{BASE_YEAR + 1}-01-01"), None)
        contracts.append({**c, "last": last, "prev": prev, "change_pct": (last / prev - 1) * 100 if prev else None, "as_of": at,
                          "ytd_pct": (last / start - 1) * 100 if start else None, "series": series[-250:]})
    return [{"section": "hormuz", "pulled_at": transits["pulled_at"], "through": days[-1][0], "days": days, "eia": EIA,
             "summary": summary(days), "contracts": contracts, "quotes_at": quotes.get("pulled_at"), "live": _load("live")}]
