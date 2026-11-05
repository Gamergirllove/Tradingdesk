"""AGGR-style aggregated BTC spot flow: trades and books from several spot exchanges merged in code.

aggr.trade is a browser app that merges live trades from many exchanges; it has no API, so the desk does the
same aggregation itself from the exchanges' public REST endpoints. The venues are ones reachable from a US
server and part of the CF Benchmarks BRTI index that Kalshi BTC markets settle on: Coinbase, Kraken,
Bitstamp, Gemini. Every number here is computed from data before now; the judge only reads the results.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

import httpx

from .. import logs
from ..util import fnum, parse_ts, r

BIG_BTC = 1.0


class SpotFlow:
    def __init__(self, poll_s: float = 10.0, venues: list[str] | None = None):
        self.poll_s = poll_s
        self.venues = venues or ["coinbase", "kraken", "bitstamp", "gemini"]
        self.trades: deque = deque(maxlen=60000)     # (ts, price, size_btc, +1 taker buy / -1 taker sell, venue)
        self.seen: dict[str, set] = {v: set() for v in self.venues}
        self.books: dict[str, tuple[float, list, list]] = {}   # venue -> (ts, bids[(p,s)], asks[(p,s)])
        self.last_ok: dict[str, float] = {}
        self.kraken_since: str | None = None
        self.gemini_since: int | None = None
        self.last_log = 0.0
        self.started: float | None = None   # windows are only reported once fully covered since start
        self._http = httpx.AsyncClient(timeout=8, headers={"User-Agent": "trading-desk/1.0"})

    async def close(self):
        await self._http.aclose()

    # ---------- fetchers (each returns list of (ts, price, size, dir)) ----------
    async def _coinbase(self):
        r_ = await self._http.get("https://api.exchange.coinbase.com/products/BTC-USD/trades", params={"limit": 1000})
        out = []
        for t in r_.json():
            tid = t["trade_id"]
            if tid in self.seen["coinbase"]:
                continue
            self.seen["coinbase"].add(tid)
            # Coinbase reports the MAKER side; the taker is the opposite.
            out.append((parse_ts(t["time"]), float(t["price"]), float(t["size"]), -1 if t["side"] == "buy" else 1))
        b = (await self._http.get("https://api.coinbase.com/api/v3/brokerage/market/product_book",
                                  params={"product_id": "BTC-USD", "limit": 200})).json().get("pricebook", {})
        self.books["coinbase"] = (time.time(), [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])],
                                  [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])])
        return out

    async def _kraken(self):
        params = {"pair": "XBTUSD", "count": 1000}
        if self.kraken_since:
            params["since"] = self.kraken_since
        j = (await self._http.get("https://api.kraken.com/0/public/Trades", params=params)).json()
        res = j.get("result") or {}
        self.kraken_since = res.get("last", self.kraken_since)
        rows = next((v for k, v in res.items() if k != "last"), [])
        out = []
        for t in rows:
            tid = t[6] if len(t) > 6 else f"{t[2]}:{t[0]}:{t[1]}"
            if tid in self.seen["kraken"]:
                continue
            self.seen["kraken"].add(tid)
            out.append((float(t[2]), float(t[0]), float(t[1]), 1 if t[3] == "b" else -1))
        d = (await self._http.get("https://api.kraken.com/0/public/Depth", params={"pair": "XBTUSD", "count": 200})).json()
        bk = next(iter((d.get("result") or {}).values()), {})
        self.books["kraken"] = (time.time(), [(float(p), float(s)) for p, s, *_ in bk.get("bids", [])],
                                [(float(p), float(s)) for p, s, *_ in bk.get("asks", [])])
        return out

    async def _bitstamp(self):
        rows = (await self._http.get("https://www.bitstamp.net/api/v2/transactions/btcusd/",
                                     params={"time": "minute"})).json()
        out = []
        for t in rows:
            if t["tid"] in self.seen["bitstamp"]:
                continue
            self.seen["bitstamp"].add(t["tid"])
            out.append((float(t["date"]), float(t["price"]), float(t["amount"]), 1 if str(t["type"]) == "0" else -1))
        return out

    async def _gemini(self):
        params = {"limit_trades": 500}
        if self.gemini_since:
            params["since_tid"] = self.gemini_since
        rows = (await self._http.get("https://api.gemini.com/v1/trades/btcusd", params=params)).json()
        out = []
        for t in rows:
            tid = t["tid"]
            self.gemini_since = max(self.gemini_since or 0, tid)
            if tid in self.seen["gemini"]:
                continue
            self.seen["gemini"].add(tid)
            out.append((t["timestampms"] / 1000, float(t["price"]), float(t["amount"]), 1 if t["type"] == "buy" else -1))
        b = (await self._http.get("https://api.gemini.com/v1/book/btcusd",
                                  params={"limit_bids": 200, "limit_asks": 200})).json()
        self.books["gemini"] = (time.time(), [(float(x["price"]), float(x["amount"])) for x in b.get("bids", [])],
                                [(float(x["price"]), float(x["amount"])) for x in b.get("asks", [])])
        return out

    async def poll_once(self):
        if self.started is None:
            self.started = time.time()
        fns = {"coinbase": self._coinbase, "kraken": self._kraken, "bitstamp": self._bitstamp, "gemini": self._gemini}
        res = await asyncio.gather(*(fns[v]() for v in self.venues if v in fns), return_exceptions=True)
        now = time.time()
        for v, rows in zip([v for v in self.venues if v in fns], res):
            if isinstance(rows, Exception):
                continue
            self.last_ok[v] = now
            for ts, p, s, d in rows:
                if ts and s > 0:
                    self.trades.append((ts, p, s, d, v))
            if len(self.seen[v]) > 50000:
                self.seen[v] = set(list(self.seen[v])[-20000:])
        cutoff = now - 3 * 3600
        while self.trades and self.trades[0][0] < cutoff:
            self.trades.popleft()
        if now - self.last_log >= 60:
            self.last_log = now
            f = self.features(now)
            logs.write("spot", {k: f.get(k) for k in ("spot_vwap_1m", "spot_cvd_5m_btc", "spot_book_imb_10bps",
                                                      "flow_venues")})

    async def run(self, stop: asyncio.Event):
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception as e:     # a feed problem never stops the desk; features just go null
                logs.write("events", {"kind": "spot_flow_error", "error": repr(e)[:200]})
            await asyncio.sleep(self.poll_s)

    # ---------- features ----------
    def features(self, at: float | None = None) -> dict:
        at = at or time.time()
        live = [v for v in self.venues if at - self.last_ok.get(v, 0) < 60]
        f: dict = {"flow_venues": len(live)}
        if not live:
            return f | {k: None for k in ("spot_cvd_1m_btc", "spot_cvd_5m_btc", "spot_cvd_15m_btc", "spot_buy_share_5m",
                                         "spot_vol_5m_vs_60m", "spot_big_net_5m", "spot_book_imb_10bps",
                                         "spot_book_imb_50bps", "spot_vwap_1m")}
        tr = [t for t in self.trades if t[0] <= at and t[4] in live]

        covered = at - (self.started or at)

        def win(mins):
            return [t for t in tr if t[0] >= at - mins * 60]

        for m in (1, 5, 15):
            w = win(m)
            f[f"spot_cvd_{m}m_btc"] = r(sum(t[2] * t[3] for t in w), 3) if covered >= m * 60 else None
        w5 = win(5)
        v5 = sum(t[2] for t in w5)
        ok5 = covered >= 300
        f["spot_buy_share_5m"] = r(sum(t[2] for t in w5 if t[3] > 0) / v5, 3) if (v5 > 0 and ok5) else None
        v60 = sum(t[2] for t in win(60))
        f["spot_vol_5m_vs_60m"] = r(v5 / (v60 / 12), 3) if (v60 > 0 and covered >= 3600) else None
        f["spot_big_net_5m"] = r(sum(t[2] * t[3] for t in w5 if t[2] >= BIG_BTC), 3) if ok5 else None
        w1 = win(1)
        q1 = sum(t[2] for t in w1)
        f["spot_vwap_1m"] = r(sum(t[1] * t[2] for t in w1) / q1, 2) if q1 > 0 else None
        for bps in (10, 50):
            bid = ask = 0.0
            for v in live:
                b = self.books.get(v)
                if not b or at - b[0] > 60 or not b[1] or not b[2]:
                    continue
                mid = (b[1][0][0] + b[2][0][0]) / 2
                lo, hi = mid * (1 - bps / 1e4), mid * (1 + bps / 1e4)
                bid += sum(s for p, s in b[1] if p >= lo)
                ask += sum(s for p, s in b[2] if p <= hi)
            f[f"spot_book_imb_{bps}bps"] = r((bid - ask) / (bid + ask), 3) if bid + ask > 0 else None
        return f

    def minute_vwaps(self, at: float | None = None, n: int = 3) -> list[float | None]:
        """VWAP of the last n one-minute windows across live venues, newest first."""
        at = at or time.time()
        live = {v for v in self.venues if at - self.last_ok.get(v, 0) < 60}
        out = []
        for i in range(n):
            hi, lo = at - 60 * i, at - 60 * (i + 1)
            w = [t for t in self.trades if lo < t[0] <= hi and t[4] in live]
            q = sum(t[2] for t in w)
            out.append(sum(t[1] * t[2] for t in w) / q if q > 0 else None)
        return out
