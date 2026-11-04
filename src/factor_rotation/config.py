import os
from pathlib import Path
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def _load_dotenv() -> None:
    """FR_* settings from .env in the working directory, so manual runs match the scheduled ones.
    Variables already set in the environment win."""
    path = Path(".env")
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.startswith("FR_") and key not in os.environ:
            os.environ[key] = value.strip().strip('"')


_load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    data_root: str = field(default_factory=lambda: _env("FR_DATA_ROOT", "data"))
    ib_host: str = field(default_factory=lambda: _env("FR_IB_HOST", "127.0.0.1"))
    ib_port: int = field(default_factory=lambda: int(_env("FR_IB_PORT", "4001")))
    ib_client_id: int = field(default_factory=lambda: int(_env("FR_IB_CLIENT_ID", "17")))
    market_data_type: int = field(default_factory=lambda: int(_env("FR_MARKET_DATA_TYPE", "2")))
    max_lines: int = field(default_factory=lambda: int(_env("FR_MAX_LINES", "90")))
    tick_timeout_s: float = field(default_factory=lambda: float(_env("FR_TICK_TIMEOUT_S", "6")))
    risk_free_rate: float = field(default_factory=lambda: float(_env("FR_RISK_FREE_RATE", "0.038")))
    etf_max_dte: int = field(default_factory=lambda: int(_env("FR_ETF_MAX_DTE", "120")))
    stock_max_dte: int = field(default_factory=lambda: int(_env("FR_STOCK_MAX_DTE", "75")))
    etf_strike_band: float = field(default_factory=lambda: float(_env("FR_ETF_STRIKE_BAND", "0.10")))
    stock_strike_band: float = field(default_factory=lambda: float(_env("FR_STOCK_STRIKE_BAND", "0.15")))
    strike_sigmas: float = field(default_factory=lambda: float(_env("FR_STRIKE_SIGMAS", "2.0")))
    near_strike_band: float = field(default_factory=lambda: float(_env("FR_NEAR_STRIKE_BAND", "0.03")))
    near_expiries: int = field(default_factory=lambda: int(_env("FR_NEAR_EXPIRIES", "5")))
    monthly_expiries: int = field(default_factory=lambda: int(_env("FR_MONTHLY_EXPIRIES", "3")))


def settings() -> Settings:
    return Settings()
