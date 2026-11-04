"""Deterministic market state: every number the judge sees is computed here, from data before now."""
from __future__ import annotations

import asyncio
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field

from .. import logs
from ..util import fnum, parse_ts, r
from ..venues.kalshi import Book, KalshiClient, KalshiError, MalformedRequest


@dataclass
class TickerState:
    meta: dict = field(default_factory=dict)
    book: Book | None = None
    book_ts: float = 0.0
    hist: deque = field(default_factory=lambda: deque(maxlen=2000))     # (ts, mid, spread, bid, ask)
    trades: deque = field(default_factory=lambda: deque(maxlen=5000))   # (ts, price, count, taker_book_side)
    trade_ids: set = field(default_factory=set)
    trades_ts: float = 0.0
    last_mid_log: float = 0.0
    errors: int = 0
    backfilled: bool = False


class MarketData:
    def __init__(self, client: KalshiClient, cfg: dict):
        self.c = client
        self.cfg = cfg
        self.t: dict[str, TickerState] = {}
        self.listing: dict[str, list[dict]] = {}
        self.listing_ts: dict[str, float] = {}
        self.series_meta: dict[str, dict] = {}
        self.iv_ema: dict[str, tuple[float, float]] = {}
        self._lock = asyncio.Lock()

    def implied_vol(self, ticker: str, spot: float | None) -> float | None:
        """Slow EMA (20 min half-life) of the event ladder's implied vol."""
        ev = self.st(ticker).meta.get("event_ticker")
        if not ev:
            return None
        lst = [m for ms in self.listing.values() for m in ms]
        iv = event_iv(lst, ev, spot)
        now = time.time()
        prev = self.iv_ema.get(ev)
        if iv is None:
            return prev[0] if prev and now - prev[1] < 1800 else None
        if prev:
            alpha = 1 - 0.5 ** ((now - prev[1]) / 1200)
            iv = prev[0] + alpha * (iv - prev[0])
        self.iv_ema[ev] = (iv, now)
        return round(iv, 4)

    def st(self, ticker: str) -> TickerState:
        if ticker not in self.t:
            self.t[ticker] = TickerState()
        return self.t[ticker]

    # ---------- universe ----------
    async def universe(self, series: list[str], max_age: float = 120) -> list[dict]:
        out: list[dict] = []
        for s in series:
            if time.time() - self.listing_ts.get(s, 0) > max_age:
                try:
                    self.listing[s] = await self.c.markets(series_ticker=s, status="open")
                    self.listing_ts[s] = time.time()
                except KalshiError as e:
                    logs.write("events", {"kind": "universe_error", "series": s, "error": str(e)})
            for m in self.listing.get(s, []):
                m.setdefault("_series", s)
                self.st(m["ticker"]).meta = m
                out.append(m)
        return out

    async def series_fee(self, series: str) -> tuple[str, float]:
        if series not in self.series_meta:
            try:
                self.series_meta[series] = await self.c.series(series)
            except KalshiError:
                self.series_meta[series] = {}
        sm = self.series_meta[series]
        return sm.get("fee_type") or "quadratic", float(sm.get("fee_multiplier") or 1.0)

    # ---------- per-ticker refresh ----------
    async def backfill(self, ticker: str, minutes: int = 90) -> int:
        """Seed mid history from 1-minute candles so a restart doesn't mean a 15-minute warm-up."""
        s = self.st(ticker)
        if s.backfilled:
            return 0
        s.backfilled = True
        series = s.meta.get("_series") or ticker.split("-")[0]
        now = time.time()
        try:
            rows = await self.c.candles(series, ticker, int(now - minutes * 60), int(now))
        except KalshiError:
            return 0
        pts = []
        for c in rows:
            ts = c.get("end_period_ts")
            b = fnum((c.get("yes_bid") or {}).get("close_dollars"))
            a = fnum((c.get("yes_ask") or {}).get("close_dollars"))
            if not ts or not b or not a or a <= b:
                continue
            pts.append((float(ts), (a + b) / 2, a - b, b, a))
        old = list(s.hist)
        s.hist.clear()
        for p in sorted(pts + old):
            s.hist.append(p)
        return len(pts)

    async def refresh_book(self, ticker: str, min_interval: float = 2.0) -> Book | None:
        s = self.st(ticker)
        if not s.backfilled:
            await self.backfill(ticker)
        if s.book and time.time() - s.book_ts < min_interval:
            return s.book
        try:
            b = await self.c.orderbook(ticker, depth=10)
        except MalformedRequest:
            s.errors += 1
            raise
        except KalshiError:
            s.errors += 1
            return None
        s.errors = 0
        s.book, s.book_ts = b, b.ts
        if b.mid is not None:
            s.hist.append((b.ts, b.mid, b.spread, b.best_bid, b.best_ask))
            if b.ts - s.last_mid_log >= 60:
                s.last_mid_log = b.ts
                v30 = sum(t[2] for t in s.trades if t[0] >= b.ts - 1800) if s.trades_ts else None
                logs.write("mids", {"ticker": ticker, "mid": r(b.mid), "bid": b.best_bid, "ask": b.best_ask,
                                    "spread": r(b.spread), "v30": v30})
        return b

    async def refresh_meta(self, ticker: str) -> dict:
        try:
            m = await self.c.market(ticker)
            if m:
                self.st(ticker).meta.update(m)
        except KalshiError:
            pass
        return self.st(ticker).meta

    async def refresh_trades(self, ticker: str, min_interval: float = 20.0) -> None:
        s = self.st(ticker)
        if time.time() - s.trades_ts < min_interval:
            return
        since = int((s.trades[-1][0] if s.trades else time.time() - 3 * 3600) - 1)
        try:
            rows = await self.c.trades(ticker, min_ts=since, limit=1000)
        except KalshiError:
            return
        s.trades_ts = time.time()
        new = []
        for t in rows:
            tid = t.get("trade_id")
            if tid in s.trade_ids:
                continue
            ts = parse_ts(t.get("created_time"))
            if ts is None:
                continue
            s.trade_ids.add(tid)
            new.append((ts, fnum(t.get("yes_price_dollars"), 0.0), fnum(t.get("count_fp"), 0.0),
                        t.get("taker_book_side") or ("bid" if t.get("taker_side") == "yes" else "ask")))
        for row in sorted(new):
            s.trades.append(row)
        if len(s.trade_ids) > 20000:
            s.trade_ids = {t for t in list(s.trade_ids)[-5000:]}

    # ---------- features ----------
    def features(self, ticker: str, at: float | None = None) -> dict:
        at = at or time.time()
        s = self.st(ticker)
        b = s.book
        f: dict = {"ticker": ticker}
        if not b or b.mid is None:
            f["book_ok"] = False
            return f
        f["book_ok"] = True
        f["state_age_s"] = r(at - s.book_ts, 1)
        f["bid"], f["ask"], f["mid"] = b.best_bid, b.best_ask, r(b.mid)
        f["spread_c"] = r((b.spread or 0) * 100, 2)
        top_b = sum(sz for _, sz in b.bids[:3])
        top_a = sum(sz for _, sz in b.asks[:3])
        f["bid_sz"], f["ask_sz"] = r(b.bids[0][1], 0), r(b.asks[0][1], 0)
        f["top3_bid"], f["top3_ask"] = r(top_b, 0), r(top_a, 0)
        f["imbalance3"] = r((top_b - top_a) / (top_b + top_a), 3) if top_b + top_a > 0 else None
        bsz, asz = b.bids[0][1], b.asks[0][1]
        f["microprice"] = r((b.best_bid * asz + b.best_ask * bsz) / (bsz + asz), 4) if bsz + asz > 0 else None

        hist = [h for h in s.hist if h[0] <= at]
        mid = b.mid

        def mid_at(t0: float) -> float | None:
            cands = [h for h in hist if h[0] <= t0]
            if not cands or t0 - cands[-1][0] > 300:
                return None
            return cands[-1][1]

        for w in (5, 15, 60):
            m0 = mid_at(at - w * 60)
            f[f"ret_{w}m_c"] = r((mid - m0) * 100, 2) if m0 is not None else None
        # 1-minute sampled mid changes over 30 minutes
        samples = []
        t0 = at - 30 * 60
        last_bucket = None
        for ts, m, *_ in hist:
            if ts < t0:
                continue
            bucket = int(ts // 60)
            if bucket != last_bucket:
                samples.append(m)
                last_bucket = bucket
        diffs = [(b2 - a2) * 100 for a2, b2 in zip(samples, samples[1:])]
        f["rv_30m_c"] = r(statistics.pstdev(diffs), 3) if len(diffs) >= 4 else None
        h60 = [h[1] for h in hist if h[0] >= at - 3600]
        if len(h60) >= 3:
            f["hi_60m"], f["lo_60m"] = r(max(h60)), r(min(h60))
            f["dist_hi_c"], f["dist_lo_c"] = r((max(h60) - mid) * 100, 2), r((mid - min(h60)) * 100, 2)
        else:
            f["hi_60m"] = f["lo_60m"] = f["dist_hi_c"] = f["dist_lo_c"] = None
        sp10 = [h[2] for h in hist if h[0] >= at - 600 and h[2] is not None]
        f["spread_avg_10m_c"] = r(statistics.mean(sp10) * 100, 2) if sp10 else None

        tr = [t for t in s.trades if t[0] <= at]
        have_trades = s.trades_ts > 0

        def vol(mins: int) -> float | None:
            if not have_trades:
                return None
            return r(sum(t[2] for t in tr if t[0] >= at - mins * 60), 0)

        f["vol_5m"], f["vol_30m"], f["vol_60m"] = vol(5), vol(30), vol(60)
        t15 = [t for t in tr if t[0] >= at - 900]
        tot15 = sum(t[2] for t in t15)
        f["taker_buy_share_15m"] = r(sum(t[2] for t in t15 if t[3] == "bid") / tot15, 3) if tot15 > 0 else None
        v24 = fnum(s.meta.get("volume_24h_fp"))
        f["vol_24h"] = r(v24, 0)
        pace30 = (v24 / 48.0) if v24 else None
        f["vol_30m_vs_pace"] = r(f["vol_30m"] / pace30, 3) if (pace30 and f["vol_30m"] is not None) else None
        f["open_interest"] = r(fnum(s.meta.get("open_interest_fp")), 0)
        close = parse_ts(s.meta.get("close_time"))
        f["minutes_to_close"] = r((close - at) / 60, 1) if close else None
        f["strike_type"] = s.meta.get("strike_type")
        f["floor_strike"] = fnum(s.meta.get("floor_strike"))
        f["cap_strike"] = fnum(s.meta.get("cap_strike"))
        return f


def implied_sigma(floor: float, spot: float, minutes: float, price: float) -> float | None:
    """Invert P(S_T > K) for an in-the-money 'greater' strike (monotone in sigma there). Bisection."""
    if not (spot and floor and minutes and minutes > 0) or spot < floor or not (0.5 <= price <= 0.97):
        return None
    lo, hi = 0.03, 4.0
    for _ in range(60):
        mid = (lo + hi) / 2
        p = fair_yes("greater", floor, None, spot, mid, minutes)
        if p is None:
            return None
        if p > price:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2, 4)


def event_iv(listing: list[dict], event_ticker: str, spot: float | None, at: float | None = None) -> float | None:
    """Market-implied vol from the event's strike ladder, so fair value is consistent with the market's own smile
    and edges come from spot moving faster than the book, not from a wrong vol guess."""
    if not spot:
        return None
    at = at or time.time()
    best = None
    ladder = [m for m in listing if m.get("event_ticker") == event_ticker]
    if len(ladder) < 3:
        return None     # one market can't imply its own vol without making its edge zero by construction
    for m in ladder:
        if m.get("strike_type") not in ("greater", "greater_or_equal"):
            continue
        k = fnum(m.get("floor_strike"))
        b, a = fnum(m.get("yes_bid_dollars")), fnum(m.get("yes_ask_dollars"))
        close = parse_ts(m.get("close_time"))
        if not k or not b or not a or a <= b or a - b > 0.04 or not close or k > spot:
            continue
        mid = (a + b) / 2
        if 0.52 <= mid <= 0.85 and (best is None or abs(mid - 0.65) < abs(best[1] - 0.65)):
            best = (k, mid, (close - at) / 60)
    if not best:
        return None
    return implied_sigma(best[0], spot, best[2], best[1])


def fair_yes(strike_type: str | None, floor: float | None, cap: float | None, spot: float | None,
             sigma_ann: float | None, minutes: float | None) -> float | None:
    """Zero-drift lognormal probability that the market resolves YES. Pure arithmetic, no model calls."""
    if not spot or not sigma_ann or not minutes or minutes <= 0:
        return None
    T = minutes / 525600.0
    sig = max(0.05, min(3.0, sigma_ann))
    sd = sig * math.sqrt(T)

    def p_above(k: float) -> float:
        d2 = (math.log(spot / k) - 0.5 * sig * sig * T) / sd
        return 0.5 * (1 + math.erf(d2 / math.sqrt(2)))

    try:
        if strike_type in ("greater", "greater_or_equal") and floor:
            return p_above(floor)
        if strike_type in ("less", "less_or_equal") and cap:
            return 1 - p_above(cap)
        if strike_type == "between" and floor and cap:
            return max(0.0, p_above(floor) - p_above(cap))
    except (ValueError, ZeroDivisionError):
        return None
    return None
