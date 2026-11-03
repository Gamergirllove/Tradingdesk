"""Real orders on Kalshi (demo or live). Outcomes are always read back from the exchange:
confidence is not permission, and an ack is not a fill."""
from __future__ import annotations

import time

from .. import logs
from ..util import fnum, new_id
from ..venues.kalshi import Book, KalshiClient, KalshiError, MalformedRequest
from .base import Ack, Pos


class KalshiBroker:
    def __init__(self, client: KalshiClient, mode: str):
        assert mode in ("demo", "live")
        self.c = client
        self.mode = mode
        self._pos_cache: tuple[float, dict[str, Pos]] = (0.0, {})
        self._bal_cache: tuple[float, dict] = (0.0, {})
        self.owned: dict[str, dict] = {}       # order_id -> info for orders this desk placed

    async def place(self, it, book: Book | None) -> Ack:
        coid = new_id("d_")
        try:
            r = await self.c.create_order(it.ticker, it.side, it.count, it.price, coid, post_only=it.post_only,
                                          tif=it.tif, reduce_only=it.reduce_only)
        except MalformedRequest as e:
            logs.write("orders", {"kind": "rejected", "mode": self.mode, "intent": it.to_dict(), "error": str(e)})
            return Ack(False, client_order_id=coid, status="rejected", message=str(e)[:300])
        except KalshiError as e:
            logs.write("orders", {"kind": "error", "mode": self.mode, "intent": it.to_dict(), "error": str(e)})
            return Ack(False, client_order_id=coid, status="error", message=str(e)[:300])
        oid = r.get("order_id")
        filled = fnum(r.get("fill_count"), 0.0)
        remaining = fnum(r.get("remaining_count"), 0.0)
        status = "filled" if remaining == 0 and filled > 0 else ("partial" if filled else "resting")
        if remaining == 0 and filled == 0:
            status = "canceled"
        if oid and remaining > 0:
            self.owned[oid] = {"ticker": it.ticker, "side": it.side, "price": it.price, "remaining": remaining,
                               "ts": time.time(), "bot": it.bot, "tag": it.tag}
        self._pos_cache = (0.0, {})
        return Ack(True, order_id=oid, client_order_id=coid, status=status, filled=filled,
                   avg_price=it.price if filled else None)

    async def cancel(self, order_id: str, ticker: str | None = None) -> bool:
        try:
            await self.c.cancel_order(order_id, ticker or self.owned.get(order_id, {}).get("ticker", ""))
            self.owned.pop(order_id, None)
            return True
        except MalformedRequest:
            self.owned.pop(order_id, None)
            return False
        except KalshiError:
            return False

    async def cancel_all(self, ticker: str | None = None) -> int:
        n = 0
        for o in await self.open_orders(ticker):
            n += await self.cancel(o["order_id"], o["ticker"])
        return n

    async def open_orders(self, ticker: str | None = None) -> list[dict]:
        try:
            rows = await self.c.resting_orders(ticker)
        except KalshiError:
            return [dict(order_id=k, **v) for k, v in self.owned.items() if ticker is None or v["ticker"] == ticker]
        out = []
        live_ids = set()
        for o in rows:
            oid = o.get("order_id")
            if oid not in self.owned:
                if not str(o.get("client_order_id") or "").startswith("d_"):
                    continue    # only manage orders this desk placed
                self.owned[oid] = {"ticker": o.get("ticker"), "bot": None, "tag": "recovered", "ts": time.time()}
            live_ids.add(oid)
            side = o.get("book_side") or ("bid" if o.get("outcome_side") == "yes" else "ask")
            out.append({"order_id": oid, "ticker": o.get("ticker"), "side": side,
                        "price": fnum(o.get("yes_price_dollars"), 0.0),
                        "remaining": fnum(o.get("remaining_count_fp"), 0.0), **{k: self.owned[oid].get(k) for k in ("bot", "tag")}})
        for oid in list(self.owned):
            if oid not in live_ids and (ticker is None or self.owned[oid]["ticker"] == ticker):
                self.owned.pop(oid)
        return out

    async def positions(self) -> dict[str, Pos]:
        ts, cache = self._pos_cache
        if time.time() - ts < 5:
            return cache
        try:
            rows = await self.c.positions()
        except KalshiError:
            return cache
        out = {}
        for p in rows:
            q = fnum(p.get("position_fp"), 0.0)
            if not q:
                continue
            exp = fnum(p.get("market_exposure_dollars"), 0.0)
            avg = (exp / q) if q > 0 else (1 - exp / -q)
            out[p["ticker"]] = Pos(qty=q, avg=round(avg, 4), realized=fnum(p.get("realized_pnl_dollars"), 0.0),
                                   fees=fnum(p.get("fees_paid_dollars"), 0.0), extra={"exposure": exp})
        self._pos_cache = (time.time(), out)
        return out

    async def position(self, ticker: str) -> Pos:
        return (await self.positions()).get(ticker, Pos())

    async def _balance(self) -> dict:
        ts, cache = self._bal_cache
        if time.time() - ts < 5:
            return cache
        try:
            b = await self.c.balance()
            self._bal_cache = (time.time(), b)
            return b
        except KalshiError:
            return cache

    async def equity(self) -> float:
        b = await self._balance()
        return round((fnum(b.get("balance"), 0.0) + fnum(b.get("portfolio_value"), 0.0)) / 100.0, 4)

    async def available(self) -> float | None:
        b = await self._balance()
        v = fnum(b.get("balance"))
        return None if v is None else v / 100.0

    def on_market(self, ticker: str, book, new_trades):
        return None

    def settle(self, ticker: str, result: str):
        return None
