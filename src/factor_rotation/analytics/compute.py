import logging

import pandas as pd

from .. import storage
from .exposures import summarize

log = logging.getLogger(__name__)


def latest_snapshot(day: str) -> str | None:
    files = storage.list_files(f"options_underlying/date={day}")
    return files[-1].rsplit("/", 1)[-1].removesuffix(".parquet") if files else None


def compute_exposures(day: str, snapshot_id: str) -> pd.DataFrame:
    files = [f for f in storage.list_files(f"options_raw/date={day}") if f.endswith(f"/{snapshot_id}.parquet")]
    summaries = []
    for path in files:
        raw = storage.read(path)
        if raw.empty:
            continue
        try:
            summary, by_strike = summarize(raw)
        except Exception:
            log.exception("exposures_failed path=%s", path)
            continue
        summaries.append(summary)
        underlying = path.split("underlying=")[1].split("/")[0]
        storage.write(by_strike, f"exposures_by_strike/date={day}/underlying={underlying}/{snapshot_id}.parquet")
    df = pd.DataFrame(summaries)
    if not df.empty:
        storage.write(df, f"exposures/date={day}/{snapshot_id}.parquet")
    log.info("exposures_done date=%s snapshot=%s underlyings=%d", day, snapshot_id, len(df))
    return df
