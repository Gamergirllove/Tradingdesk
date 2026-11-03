from __future__ import annotations

import math
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def now() -> float:
    return time.time()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def local_day(tz: str = "America/Los_Angeles", ts: float | None = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), ZoneInfo(tz))
    return dt.strftime("%Y-%m-%d")


def local_hm(tz: str = "America/Los_Angeles") -> tuple[int, int, int]:
    dt = datetime.now(ZoneInfo(tz))
    return dt.weekday(), dt.hour, dt.minute


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def fnum(x, default: float | None = None) -> float | None:
    try:
        if x is None or x == "":
            return default
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(float(x), nd)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def ceil_cent(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100.0


def kalshi_fee(price: float, count: float, fee_type: str = "quadratic", multiplier: float = 1.0,
               maker: bool = False) -> float:
    """Kalshi general fee schedule: 0.07*C*P*(1-P) taker; 0.0175*C*P*(1-P) maker on maker-fee series."""
    p = clamp(price, 0.0, 1.0)
    if maker:
        if fee_type and "maker" in fee_type:
            return ceil_cent(0.0175 * multiplier * count * p * (1 - p))
        return 0.0
    if fee_type == "flat":
        return ceil_cent(0.01 * count * multiplier)
    return ceil_cent(0.07 * multiplier * count * p * (1 - p))
