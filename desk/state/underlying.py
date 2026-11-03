"""Spot feed for crypto event markets (Coinbase public candles; no key)."""
from __future__ import annotations

import math
import statistics
import time

import httpx

from ..util import fnum, r

CB = "https://api.coinbase.com/api/v3/brokerage/market/products/{p}/candles"


class Underlying:
    def __init__(self, products: dict[str, str], poll_seconds: int = 30):
        self.products = products          # series -> product id
        self.poll = poll_seconds
        self.candles: dict[str, list[tuple[float, float]]] = {}   # product -> [(ts, close)] ascending
        self.ts: dict[str, float] = {}
        self._http = httpx.AsyncClient(timeout=10)

    def product_for(self, series: str | None) -> str | None:
        return self.products.get(series or "")

    async def refresh(self, product: str) -> None:
        if time.time() - self.ts.get(product, 0) < self.poll:
            return
        try:
            resp = await self._http.get(CB.format(p=product), params={"granularity": "ONE_MINUTE", "limit": 300})
            resp.raise_for_status()
            rows = resp.json().get("candles") or []
        except (httpx.HTTPError, ValueError):
            return
        pts = sorted((float(c["start"]), float(c["close"])) for c in rows if c.get("close"))
        if pts:
            self.candles[product] = pts
            self.ts[product] = time.time()

    def features(self, product: str | None, at: float | None = None) -> dict:
        at = at or time.time()
        if not product or product not in self.candles:
            return {"spot": None, "spot_age_s": None}
        pts = [p for p in self.candles[product] if p[0] + 60 <= at + 1]   # closed candles only
        if len(pts) < 10:
            return {"spot": None, "spot_age_s": None}
        spot = pts[-1][1]
        out = {"spot": r(spot, 2), "spot_age_s": r(at - (pts[-1][0] + 60), 0)}

        def ret(mins: int):
            older = [p for p in pts if p[0] <= pts[-1][0] - mins * 60]
            return r((spot / older[-1][1] - 1) * 1e4, 1) if older else None

        out["spot_ret_5m_bps"], out["spot_ret_15m_bps"], out["spot_ret_60m_bps"] = ret(5), ret(15), ret(60)
        closes = [p[1] for p in pts[-121:]]
        lr = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
        out["spot_rv_ann"] = r(statistics.pstdev(lr) * math.sqrt(525600), 3) if len(lr) >= 20 else None
        return out

    async def close(self):
        await self._http.aclose()


def spot_value(feat: dict) -> float | None:
    return fnum(feat.get("spot"))
