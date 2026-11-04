"""A local copy of the Factor Rotation Ledger dashboard.

dashboard/page.html is the same page published to claude.ai. Online, the Dashboard runtime supplies the
`dash` API, the design tokens and the data files; here dashboard/shim.js and dashboard/tokens.css stand in,
and the ledger and tonight's market rows are embedded, so the result opens straight from disk with no server.
The one outside dependency is d3 from a CDN, so charts need a network connection.
"""

import json
import logging
from datetime import date, datetime
from pathlib import Path

from . import journal
from .config import NY

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD = ROOT / "dashboard"
DEFAULT_OUT = DASHBOARD / "local.html"
D3 = "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"


def _latest_brief() -> date | None:
    briefs = sorted((journal.JOURNAL / "briefs").glob("*.json"))
    return date.fromisoformat(briefs[-1].stem) if briefs else None


def _market_rows(as_of: date | None) -> tuple[list[dict] | None, str]:
    """Tonight's market rows, or None with the reason when they can't be built here (no brief yet, or the
    option data lives on the collector's disk)."""
    as_of = as_of or _latest_brief()
    if as_of is None:
        return None, "no brief in journal/briefs"
    try:
        from . import market
        return market.build(as_of), f"brief {as_of}"
    except Exception as e:  # missing market data on this machine shows as a failed panel, not a failed build
        log.warning("market_rows_failed as_of=%s error=%r", as_of, e)
        return None, f"market rows unavailable for {as_of}: {e}"


def _embed(obj) -> str:
    # Escape "</" so embedded text can't end the script element early.
    return json.dumps(obj, separators=(",", ":"), default=journal._json_default).replace("</", "<\\/")


def build(out: Path = DEFAULT_OUT, as_of: date | None = None, marks: dict | None = None) -> dict:
    ledger = journal.ledger(marks)
    market, market_note = _market_rows(as_of)
    page = (DASHBOARD / "page.html").read_text()
    built = datetime.now(NY).strftime("%b %-d, %Y %-I:%M %p ET")
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Factor Rotation Ledger</title>
<style>
{(DASHBOARD / "tokens.css").read_text()}
</style>
<script src="{D3}"></script>
<script>window.__DASH_DATA__ = {{"ledger": {_embed(ledger)}, "market": {_embed(market)}}};</script>
<script>
{(DASHBOARD / "shim.js").read_text()}
</script>
</head>
<body>
<p class="local-banner">Local copy, built {built} from the journal ({len(ledger)} ideas; {market_note}). Rebuild with <code>uv run fr dashboard</code>.</p>
{page}
</body>
</html>
"""
    out.write_text(html)
    return {"out": str(out), "ideas": len(ledger), "market_rows": len(market) if market is not None else None,
            "market": market_note}
