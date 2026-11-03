"""Paper broker with the same interface as the live one. Fills are simulated against the live Kalshi book.

Resting orders fill only when the market trades through or crosses the price (conservative: a share of
the traded volume at our price, since queue position is unknown).
"""
from __future__ import annotations

import json
import time

from .. import logs
from ..config import data_dir
from ..util import kalshi_fee, new_id
from ..venues.kalshi import Book
from .base import Ack, Pos


class PaperBroker:
    def __init__(self, name: str, bankroll: float = 50.0, fill_fraction: float = 0.3, mode: str = "paper"):
        self.name = name
        self.mode = mode
        self.fill_fraction = fill_fraction
        self.path = data_dir() / f"paper_{name}.json"
        self.cash = bankroll
        self.pos: dict[str, Pos] = {}
        self.orders: dict[str, dict] = {}
        self.fills: list[dict] = []
        self.marks: dict[str, float] = {}
        self.fee_info: dict[str, tuple[str, float]] = {}
        self._load()

    # ---------- persistence ----------
    def _load(self):
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                self.cash = d["cash"]
                self.pos = {k: Pos(**v) for k, v in d.get("pos", {}).items()}
                self.orders = d.get("orders", {})
                self.fills = d.get("fills", [])[-500:]
                self.marks = d.get("marks", {})
            except (ValueError, KeyError, TypeError):
                pass

    def _save(self):
        d = {"cash": self.cash, "pos": {k: v.__dict__ for k, v in self.pos.items() if v.qty or v.realized},
             "orders": self.orders, "fills": self.fills[-500:], "marks": self.marks}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(self.path)

    # ---------- accounting ----------
    def _apply_fill(self, ticker: str, side: str, qty: float, price: float, maker: bool, order_id: str):
        ft, fm = self.fee_info.get(ticker, ("quadratic", 1.0))
        fee = kalshi_fee(price, qty, ft, fm, maker=maker)
        p = self.pos.setdefault(ticker, Pos())
        signed = qty if side == "bid" else -qty
        # cash: buying YES pays price; buying NO pays (1-price). Closing returns the other leg.
        if p.qty == 0 or (p.qty > 0) == (signed > 0):
            new_qty = p.qty + signed
            p.avg = (p.avg * abs(p.qty) + price * qty) / abs(new_qty)
            p.qty = new_qty
            self.cash -= qty * (price if side == "bid" else 1 - price)
        else:
            close = min(abs(p.qty), qty)
            if p.qty > 0:      # selling YES we hold
                pnl = (price - p.avg) * close
                self.cash += close * p.avg + pnl
            else:              # buying back YES against NO we hold
                pnl = (p.avg - price) * close
                self.cash += close * (1 - p.avg) + pnl
            p.realized += pnl
            p.qty += close if p.qty < 0 else -close
            rem = qty - close
            if p.qty == 0:
                p.avg = 0.0
            if rem > 0:
                p.qty = rem if side == "bid" else -rem
                p.avg = price
                self.cash -= rem * (price if side == "bid" else 1 - price)
        self.cash -= fee
        p.fees += fee
        f = {"ts": time.time(), "ticker": ticker, "side": side, "qty": qty, "price": price, "maker": maker,
             "fee": fee, "order_id": order_id, "broker": self.name}
        self.fills.append(f)
        logs.write("fills", {**f, "mode": self.mode})
        return fee

    # ---------- interface ----------
    async def place(self, it, book: Book | None) -> Ack:
        if it.ticker not in self.fee_info:
            self.fee_info[it.ticker] = (it.fee_type, it.fee_mult)
        oid = new_id("p_")
        if book is None:
            return Ack(False, message="no book")
        remaining = it.count
        filled, cost, fees = 0.0, 0.0, 0.0
        levels = book.asks if it.side == "bid" else book.bids
        crosses = (lambda px: px <= it.price) if it.side == "bid" else (lambda px: px >= it.price)
        if it.post_only and levels and crosses(levels[0][0]):
            return Ack(False, message="post-only would cross", status="rejected")
        if not it.post_only:
            for px, sz in levels:
                if remaining <= 0 or not crosses(px):
                    break
                take = min(sz, remaining)
                fees += self._apply_fill(it.ticker, it.side, take, px, False, oid)
                filled += take
                cost += take * px
                remaining -= take
        status = "filled" if remaining <= 0 else ("partial" if filled else "resting")
        if remaining > 0 and it.tif == "good_till_canceled":
            self.orders[oid] = {"order_id": oid, "ticker": it.ticker, "side": it.side, "price": it.price,
                                "remaining": remaining, "ts": time.time(), "bot": it.bot, "tag": it.tag}
        elif remaining > 0:
            status = "filled" if filled else "canceled"
        self._save()
        return Ack(True, order_id=oid, status=status, filled=filled,
                   avg_price=(cost / filled if filled else None), fee=fees)

    async def cancel(self, order_id: str, ticker: str | None = None) -> bool:
        ok = self.orders.pop(order_id, None) is not None
        self._save()
        return ok

    async def cancel_all(self, ticker: str | None = None) -> int:
        ids = [k for k, o in self.orders.items() if ticker is None or o["ticker"] == ticker]
        for k in ids:
            self.orders.pop(k)
        self._save()
        return len(ids)

    async def open_orders(self, ticker: str | None = None) -> list[dict]:
        return [o for o in self.orders.values() if ticker is None or o["ticker"] == ticker]

    async def position(self, ticker: str) -> Pos:
        return self.pos.get(ticker, Pos())

    async def positions(self) -> dict[str, Pos]:
        return {k: v for k, v in self.pos.items() if v.qty}

    async def equity(self) -> float:
        eq = self.cash
        for t, p in self.pos.items():
            if not p.qty:
                continue
            m = self.marks.get(t, p.avg)
            eq += p.qty * m if p.qty > 0 else -p.qty * (1 - m)
        return round(eq, 4)

    async def available(self) -> float | None:
        locked = sum(o["remaining"] * (o["price"] if o["side"] == "bid" else 1 - o["price"]) for o in self.orders.values())
        return round(self.cash - locked, 4)

    def on_market(self, ticker: str, book: Book | None, new_trades: list[tuple]):
        """Simulate resting fills from the latest book and trades printed since the last call."""
        if book and book.mid is not None:
            self.marks[ticker] = book.mid
        changed = False
        for oid, o in list(self.orders.items()):
            if o["ticker"] != ticker:
                continue
            px, side = o["price"], o["side"]
            fill = 0.0
            if book:
                if side == "bid" and book.best_ask is not None and book.best_ask <= px:
                    fill = o["remaining"]
                if side == "ask" and book.best_bid is not None and book.best_bid >= px:
                    fill = o["remaining"]
            if not fill:
                at_px, beyond = 0.0, False
                for ts, tpx, cnt, taker_side in new_trades:
                    if ts < o["ts"]:
                        continue
                    if side == "bid" and taker_side == "ask":
                        beyond |= tpx < px - 1e-9
                        at_px += cnt if abs(tpx - px) < 1e-9 else 0
                    if side == "ask" and taker_side == "bid":
                        beyond |= tpx > px + 1e-9
                        at_px += cnt if abs(tpx - px) < 1e-9 else 0
                fill = o["remaining"] if beyond else min(o["remaining"], float(int(at_px * self.fill_fraction)))
            if fill > 0:
                self._apply_fill(ticker, side, fill, px, True, oid)
                o["remaining"] -= fill
                if o["remaining"] <= 0:
                    self.orders.pop(oid)
                changed = True
        if changed or book:
            self._save()

    def settle(self, ticker: str, result: str):
        p = self.pos.get(ticker)
        if not p or not p.qty:
            return
        val = 1.0 if result == "yes" else 0.0
        if p.qty > 0:
            pnl = (val - p.avg) * p.qty
            self.cash += p.qty * val
        else:
            pnl = (p.avg - val) * (-p.qty)
            self.cash += (-p.qty) * (1 - val)
        p.realized += pnl
        logs.write("fills", {"ticker": ticker, "settle": result, "qty": p.qty, "pnl": round(pnl, 4),
                             "broker": self.name, "mode": self.mode})
        p.qty, p.avg = 0.0, 0.0
        self._save()
