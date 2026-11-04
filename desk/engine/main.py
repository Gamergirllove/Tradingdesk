"""The desk engine: data, bots, risk, approvals and the phone dashboard in one asyncio process.

Code calculates the state -> the judge answers the battery -> code applies policy -> the risk engine checks
-> execution places the order. This process never holds the Anthropic key.
"""
from __future__ import annotations

import asyncio
import os
import time
import traceback
from collections import deque

from .. import ledger, logs, notify, secrets
from ..book.positions import Bench, Book
from ..bots.gambler import Gambler
from ..bots.house import House
from ..broker.kalshi_broker import KalshiBroker
from ..broker.paper import PaperBroker
from ..config import data_dir, effective_mode, get_path, load_config
from ..filter.funnel import Funnel
from ..judge.client import JudgeClient
from ..policy.calibration import Calibrator
from ..risk.engine import RiskEngine
from ..state.market import MarketData
from ..state.underlying import Underlying
from ..util import local_hm
from ..venues.kalshi import KalshiClient, Signer
from .approvals import Approvals


def load_signer(prefix: str) -> Signer | None:
    kid, pem = secrets.read(f"{prefix}_key_id"), secrets.read(f"{prefix}_key.pem")
    if not kid or not pem:
        return None
    try:
        return Signer(kid, pem + "\n")
    except Exception as e:      # bad PEM never takes the desk down
        logs.write("events", {"kind": "bad_key", "which": prefix, "error": type(e).__name__})
        return None


class Desk:
    def __init__(self, cfg: dict | None = None, judge: JudgeClient | None = None):
        self.cfg = cfg or load_config()
        self.tz = get_path(self.cfg, "desk.timezone", "America/Los_Angeles")
        v = self.cfg["venue"]
        self.prod_signer = load_signer("kalshi")
        self.demo_signer = load_signer("kalshi_demo")
        self.data_client = KalshiClient(v["rest_prod"], signer=self.prod_signer, alt_base_url=v.get("rest_prod_alt"))
        self.md = MarketData(self.data_client, self.cfg)
        self.und = Underlying(self.cfg["underlying"]["products"], self.cfg["underlying"].get("poll_seconds", 30))
        j = self.cfg["judge"]
        self.judge = judge or JudgeClient(j["url"], secrets.desk_secret(), int(j.get("deadline_ms", 9000)),
                                          int(j.get("consecutive_errors_halt", 5)))
        self.risk = RiskEngine(self.cfg, self.tz)
        self.book = Book()
        self.bench = Bench(self.cfg["bench_ttl_minutes"])
        self.cal = Calibrator()
        self.funnel = Funnel(self.md, self.und, self.bench, self.judge, self.cfg,
                             lambda: {lk["ticker"] for lk in self.book.locks.values()})
        self.md_demo: MarketData | None = None
        self.funnel_demo: Funnel | None = None
        self.brokers: dict[str, object] = {}
        self.approvals = Approvals(self)
        self.bots = {"gambler": Gambler(self), "house": House(self)}
        self.started = time.time()
        self.errors: dict[str, int] = {}
        self.recent_alerts: deque = deque(maxlen=50)
        self.trade_seen: dict[str, float] = {}
        self.last_summary_day = ""
        self.last_label = 0.0
        self.judge_health: dict = {}
        self.equity: dict[str, float] = {}
        self.stop = asyncio.Event()

    # ---------- wiring ----------
    def reload(self):
        self.cfg = load_config()
        for obj in (self.risk, self.funnel, self.md, self.md_demo, self.funnel_demo):
            if obj is not None:
                obj.cfg = self.cfg
        self.bench.ttl = self.cfg["bench_ttl_minutes"]
        logs.write("events", {"kind": "config_reloaded"})

    def reload_keys(self):
        self.prod_signer = load_signer("kalshi")
        self.demo_signer = load_signer("kalshi_demo")
        self.data_client.signer = self.prod_signer
        for k in ("demo", "live"):
            self.brokers.pop(k, None)
        self.md_demo = self.funnel_demo = None

    def md_for(self, mode: str) -> MarketData:
        """The demo rung prices off the demo exchange's own books; every other rung uses production data."""
        if mode != "demo" or not self.demo_signer:
            return self.md
        if self.md_demo is None:
            self.md_demo = MarketData(KalshiClient(self.cfg["venue"]["rest_demo"], self.demo_signer), self.cfg)
        return self.md_demo

    def funnel_for(self, mode: str) -> Funnel:
        if mode != "demo" or not self.demo_signer:
            return self.funnel
        if self.funnel_demo is None:
            self.funnel_demo = Funnel(self.md_for("demo"), self.und, self.bench, self.judge, self.cfg,
                                      lambda: {lk["ticker"] for lk in self.book.locks.values()})
        return self.funnel_demo

    def broker_for(self, bot: str, mode: str):
        if mode == "shadow":
            return None
        if mode == "paper":
            key = f"paper_{bot}"
            if key not in self.brokers:
                self.brokers[key] = PaperBroker(bot, bankroll=float(get_path(self.cfg, "desk.paper_bankroll", 50)))
            return self.brokers[key]
        if mode == "demo":
            if not self.demo_signer:
                return None
            if "demo" not in self.brokers:
                self.brokers["demo"] = KalshiBroker(KalshiClient(self.cfg["venue"]["rest_demo"], self.demo_signer), "demo")
            return self.brokers["demo"]
        if mode == "live":
            if not self.prod_signer:
                return None
            if "live" not in self.brokers:
                self.brokers["live"] = KalshiBroker(KalshiClient(self.cfg["venue"]["rest_prod"], self.prod_signer,
                                                                 alt_base_url=self.cfg["venue"].get("rest_prod_alt")), "live")
            return self.brokers["live"]
        return None

    def broker_key(self, bot: str, mode: str) -> str | None:
        return {"paper": f"paper_{bot}", "demo": "demo", "live": "live"}.get(mode)

    async def total_exposure(self) -> float:
        tot = 0.0
        for b in list(self.brokers.values()):
            for p in (await b.positions()).values():
                tot += p.exposure()
        return round(tot, 4)

    async def alert(self, title: str, text: str, priority: str = "default", dedupe_s: float = 600):
        key = (title, text)
        now = time.time()
        if any(k == key and now - ts < dedupe_s for ts, k in self.recent_alerts):
            return
        self.recent_alerts.append((now, key))
        logs.write("alerts", {"title": title, "text": text, "priority": priority})
        await notify.push(title, text, priority=priority, server=self.cfg["notify"]["ntfy_server"],
                          click=self.public_url())

    def public_url(self) -> str | None:
        p = data_dir() / "public_url"
        return p.read_text().strip() if p.exists() else None

    # ---------- loops ----------
    async def data_loop(self):
        v = self.cfg["venue"]
        while not self.stop.is_set():
            for md, fn, src in ((self.md, self.funnel, "prod"), (self.md_demo, self.funnel_demo, "demo")):
                if md is None or fn is None:
                    continue
                await self._poll_source(md, fn, src, v)
            await self._settle_paper()
            await asyncio.sleep(v["poll_seconds"])

    async def _poll_source(self, md: MarketData, fn: Funnel, src: str, v: dict):
        want_mode = (lambda m: m == "demo") if src == "demo" else (lambda m: m != "demo")
        watch = {lk["ticker"] for lk in self.book.locks.values() if want_mode(lk.get("mode", "shadow"))}
        for lst in fn.watch.values():
            watch.update(lst)
        sem = asyncio.Semaphore(4)

        async def one(t):
            async with sem:
                try:
                    b = await md.refresh_book(t, min_interval=v["poll_seconds"] - 1)
                    await md.refresh_trades(t, min_interval=v["trades_poll_seconds"])
                    return t, b
                except Exception as e:
                    logs.write("events", {"kind": "data_error", "ticker": t, "source": src, "error": str(e)[:200]})
                    return t, None

        results = await asyncio.gather(*(one(t) for t in list(watch)[:12]))
        for t, book in results:
            if src == "demo" or book is None:
                continue
            st = md.st(t)
            seen = self.trade_seen.get(t, time.time() - 1)
            new = [x for x in st.trades if x[0] > seen]
            if st.trades:
                self.trade_seen[t] = max(seen, st.trades[-1][0])
            for k, b in list(self.brokers.items()):
                if k.startswith("paper_"):
                    b.on_market(t, book, new)

    async def _settle_paper(self):
        for k, b in list(self.brokers.items()):
            if not k.startswith("paper_"):
                continue
            for t, p in list((await b.positions()).items()):
                meta = self.md.st(t).meta
                if meta.get("status") in ("closed", "settled", "finalized", "determined") or \
                        (meta.get("close_time") and time.time() > (self._ts(meta.get("close_time")) or 9e18)):
                    meta = await self.md.refresh_meta(t)
                    if meta.get("result") in ("yes", "no"):
                        b.settle(t, meta["result"])

    @staticmethod
    def _ts(s):
        from ..util import parse_ts
        return parse_ts(s)

    async def bots_loop(self):
        while not self.stop.is_set():
            for name, bot in self.bots.items():
                try:
                    await bot.step()
                    self.errors[name] = 0
                except Exception as e:
                    self.errors[name] = self.errors.get(name, 0) + 1
                    logs.write("events", {"kind": "bot_exception", "bot": name, "error": repr(e)[:300],
                                          "trace": traceback.format_exc()[-1500:]})
                    if self.errors[name] >= 5:
                        self.risk.halt(f"5 consecutive exceptions: {repr(e)[:120]}", bot=name)
                        await self.alert(f"{name} halted", f"Repeated errors: {repr(e)[:200]}", "high")
                        self.errors[name] = 0
            await asyncio.sleep(2)

    async def risk_loop(self):
        while not self.stop.is_set():
            try:
                eq = {}
                for k, b in list(self.brokers.items()):
                    eq[k] = await b.equity()
                self.equity = eq
                self.risk.roll_day(eq)
                for k, e in eq.items():
                    hit, pnl = self.risk.daily_loss_breached(k, e)
                    if hit:
                        for bot in self.bots:
                            if self.broker_key(bot, effective_mode(self.cfg, bot)) == k and bot not in self.risk.state.halted_bots:
                                self.risk.halt(f"daily loss limit on {k}: {pnl:.2f} USD", bot=bot)
                                await self.alert("Daily loss limit hit", f"{bot} halted on {k} at {pnl:.2f} USD. "
                                                 "Open the desk and APPROVE to resume.", "urgent")
                await self._reconcile()
                self.judge_health = await self.judge.health()
            except Exception as e:
                logs.write("events", {"kind": "risk_loop_error", "error": repr(e)[:300]})
            await asyncio.sleep(30)

    async def _reconcile(self):
        """Outcomes are checked in code, separately from what the desk believes."""
        for k in ("demo", "live"):
            b = self.brokers.get(k)
            if not b:
                continue
            pos = await b.positions()
            held = {lk["ticker"]: bot for bot, lk in self.book.locks.items() if lk.get("mode") == k}
            for t in pos:
                if t not in held:
                    logs.write("events", {"kind": "orphan_position", "broker": k, "ticker": t, "qty": pos[t].qty})
                    await self.alert("Position the desk doesn't own", f"{k}: {t} qty {pos[t].qty}. "
                                     "Nothing was done to it.", "high")

    async def schedule_loop(self):
        from ..reports import calibration as calrep, daily
        hour = int(self.cfg["desk"].get("daily_summary_hour", 8))
        while not self.stop.is_set():
            try:
                if time.time() - self.last_label > 900:
                    self.last_label = time.time()
                    await asyncio.to_thread(calrep.label_outcomes)
                _, h, m = local_hm(self.tz)
                from ..util import local_day
                today = local_day(self.tz)
                if h == hour and self.last_summary_day != today:
                    self.last_summary_day = today
                    txt = await asyncio.to_thread(daily.build_text, self)
                    await self.alert("Desk daily summary", txt)
            except Exception as e:
                logs.write("events", {"kind": "schedule_error", "error": repr(e)[:300]})
            await asyncio.sleep(60)

    async def startup(self):
        # A malformed battery is caught again before any model call on the first ask, so halts from an
        # earlier run's in-memory disable list are cleared on restart (new code may have fixed the cause).
        for bot, why in list(self.risk.state.halted_bots.items()):
            if str(why).startswith("malformed battery"):
                self.risk.clear_halt(bot)
        for bot in self.bots:
            mode = effective_mode(self.cfg, bot)
            b = self.broker_for(bot, mode)
            if isinstance(b, KalshiBroker):
                n = await b.cancel_all()
                logs.write("events", {"kind": "startup_cancel", "broker": mode, "n": n})
        logs.write("events", {"kind": "startup", "modes": {b: effective_mode(self.cfg, b) for b in self.bots},
                              "kalshi_key": bool(self.prod_signer), "demo_key": bool(self.demo_signer)})

    async def run(self, port: int = 8080):
        import uvicorn
        from .api import create_app
        await self.startup()
        app = create_app(self)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        await asyncio.gather(server.serve(), self.data_loop(), self.bots_loop(), self.risk_loop(),
                             self.schedule_loop())

    def ai_spend(self) -> dict:
        return {"spent": ledger.spent(tz=self.tz), "cap": self.cfg["judge"].get("daily_budget_usd"),
                "by_source": ledger.by_source(tz=self.tz)}


def main():
    port = int(os.environ.get("DESK_PORT", "8080"))
    desk = Desk()
    asyncio.run(desk.run(port))


if __name__ == "__main__":
    main()
