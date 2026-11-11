"""The NYSE trading calendar, read once.

A lookup through pandas_market_calendars costs about a tenth of a second, and one dashboard build makes dozens.
This holds every session from 2018 to three years ahead and slices it. A date outside that goes to the library.
"""

from datetime import date, timedelta
from functools import lru_cache

import pandas as pd
import pandas_market_calendars as mcal


@lru_cache(maxsize=1)
def _sessions() -> pd.DatetimeIndex:
    return mcal.get_calendar("XNYS").valid_days(date(2018, 1, 1), date.today() + timedelta(days=3 * 365))


class _NYSE:
    def valid_days(self, start, end) -> pd.DatetimeIndex:
        days = _sessions()
        first, last = (pd.Timestamp(pd.Timestamp(d).date(), tz="UTC") for d in (start, end))
        if first < pd.Timestamp(2018, 1, 1, tz="UTC") or last > days[-1]:
            return mcal.get_calendar("XNYS").valid_days(start, end)
        return days[days.searchsorted(first):days.searchsorted(last, side="right")]


NYSE = _NYSE()
