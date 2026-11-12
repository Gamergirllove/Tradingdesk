"""The risk engine never delegates. Checked before every order; no model gets a vote."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .. import logs
from ..config import data_dir
from ..util import kalshi_fee, local_day, now


@dataclass
class Intent:
    bot: str
    ticker: str
    side: str                    # bid (long YES) | ask (long NO / sell YES)
    count: float
    price: float
    post_only: bool = False
    reduce_only: bool = False
    tif: str = "good_till_canceled"
    expected_edge_c: float | None = None     # computed by code, per contract
    fee_type: str = "quadratic"
    fee_mult: float = 1.0
    model_version: str | None = None
    state_ts: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    band: tuple[float, float] | None = None
    decision_id: str = ""
    tag: str = ""                # entry | exit | quote
    forced: bool = False         # human tapped Force: skips judge-side checks, never the hard limits

    def to_dict(self):
        return asdict(self)


@dataclass
class RiskState:
    day: str = ""
    halted: bool = False
    halt_reason: str = ""
    halted_bots: dict = field(default_factory=dict)       # bot -> reason
    paused_bots: dict = field(default_factory=dict)       # bot -> reason (reversible)
    pinned_version: str = ""
    day_start_equity: dict = field(default_factory=dict)  # mode -> equity at day start
    loss_extra: dict = field(default_factory=dict)        # broker key -> extra loss room added by Resume today


class RiskEngine:
    def __init__(self, cfg: dict, tz: str):
        self.cfg = cfg
        self.tz = tz
        self.state = RiskState()
        self.load()

    # ---------- persistence ----------
    def _path(self):
        return data_dir() / "risk_state.json"

    def load(self):
        p = self._path()
        if p.exists():
            try:
                self.state = RiskState(**json.loads(p.read_text()))
            except (TypeError, ValueError):
                pass
        pin = self.cfg["judge"].get("pinned_version") or ""
        if pin:
            self.state.pinned_version = pin

    def save(self):
        p = self._path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=1))
        tmp.replace(p)

    # ---------- controls ----------
    def halt(self, reason: str, bot: str | None = None):
        if bot:
            self.state.halted_bots[bot] = reason
        else:
            self.state.halted, self.state.halt_reason = True, reason
        self.save()
        logs.write("events", {"kind": "halt", "bot": bot, "reason": reason})

    def clear_halt(self, bot: str | None = None):
        if bot:
            self.state.halted_bots.pop(bot, None)
        else:
            self.state.halted, self.state.halt_reason = False, ""
        self.save()
        logs.write("events", {"kind": "halt_cleared", "bot": bot})

    def pause(self, bot: str, reason: str):
        self.state.paused_bots[bot] = reason
        self.save()

    def resume(self, bot: str):
        self.state.paused_bots.pop(bot, None)
        self.save()

    def pin(self, version: str):
        self.state.pinned_version = version
        self.save()
        logs.write("events", {"kind": "version_pinned", "version": version})

    def blocked(self, bot: str) -> str | None:
        if self.state.halted:
            return f"desk halted: {self.state.halt_reason}"
        if bot in self.state.halted_bots:
            return f"bot halted: {self.state.halted_bots[bot]}"
        if bot in self.state.paused_bots:
            return f"bot paused: {self.state.paused_bots[bot]}"
        return None

    # ---------- daily loss ----------
    def roll_day(self, equity_by_key: dict[str, float]):
        d = local_day(self.tz)
        if self.state.day != d:
            self.state.day = d
            self.state.day_start_equity = dict(equity_by_key)
            self.state.loss_extra = {}
            self.save()

    def day_pnl(self, key: str, equity: float) -> float:
        start = self.state.day_start_equity.get(key)
        return 0.0 if start is None else round(equity - start, 4)

    def base_limit(self, key: str) -> float:
        """Daily loss allowed before Resume. Ladder mode: a share of the account at the day's start."""
        lad = self.cfg["risk"].get("ladder") or {}
        start = self.state.day_start_equity.get(key)
        if lad.get("enabled") and key == "live" and start:
            return round(float(start) * float(lad.get("floor_pct", 50)) / 100, 2)
        return float(self.cfg["risk"]["daily_loss_limit_usd"])

    def loss_limit(self, key: str) -> float:
        return round(self.base_limit(key) + float(self.state.loss_extra.get(key, 0)), 2)

    def extend_loss(self, key: str, pnl: float, add: float) -> float:
        """Resume after a loss-limit halt: allow `add` more loss from where the day stands now."""
        base = self.base_limit(key)
        need = max(0.0, -pnl) + add - base
        self.state.loss_extra[key] = round(max(float(self.state.loss_extra.get(key, 0)), need), 2)
        self.save()
        logs.write("events", {"kind": "loss_limit_extended", "key": key, "pnl": pnl, "limit": self.loss_limit(key)})
        return self.loss_limit(key)

    def daily_loss_breached(self, key: str, equity: float) -> tuple[bool, float]:
        lim = self.loss_limit(key)
        start = self.state.day_start_equity.get(key)
        if start is None:
            self.state.day_start_equity[key] = equity
            self.save()
            return False, 0.0
        pnl = equity - start
        return pnl <= -lim, round(pnl, 4)

    # ---------- the check ----------
    def check(self, it: Intent, position_contracts: float, total_exposure_usd: float,
              available_usd: float | None) -> tuple[bool, str]:
        rk = self.cfg["risk"]
        checks: list[tuple[str, bool, str]] = []
        b = self.blocked(it.bot)
        shrinking = it.reduce_only or abs(position_contracts + (it.count if it.side == "bid" else -it.count)) < abs(position_contracts)
        if b and not shrinking:
            checks.append(("halt", False, b))
        checks.append(("price_band", rk["price_min"] <= it.price <= rk["price_max"] or shrinking,
                       f"price {it.price} outside [{rk['price_min']}, {rk['price_max']}]"))
        checks.append(("order_size", 0 < it.count <= rk["max_order_contracts"],
                       f"order {it.count} > max {rk['max_order_contracts']}"))
        if it.state_ts is not None:
            age = now() - it.state_ts
            checks.append(("stale_state", age <= rk["max_state_age_s"], f"state {age:.0f}s old"))
        if it.post_only and it.best_ask is not None and it.side == "bid":
            checks.append(("post_only_cross", it.price < it.best_ask, f"bid {it.price} would cross ask {it.best_ask}"))
        if it.post_only and it.best_bid is not None and it.side == "ask":
            checks.append(("post_only_cross", it.price > it.best_bid, f"ask {it.price} would cross bid {it.best_bid}"))
        signed = it.count if it.side == "bid" else -it.count
        new_pos = position_contracts + signed
        closing = it.reduce_only or (abs(new_pos) < abs(position_contracts) and
                                     (new_pos == 0 or (new_pos > 0) == (position_contracts > 0)))
        if not closing:
            if abs(new_pos) > abs(position_contracts):
                checks.append(("max_position", abs(new_pos) <= rk["max_position_contracts"],
                               f"position would be {new_pos} > {rk['max_position_contracts']}"))
                cost = it.count * (it.price if it.side == "bid" else 1 - it.price)
                checks.append(("max_exposure", total_exposure_usd + cost <= rk["max_total_exposure_usd"],
                               f"exposure {total_exposure_usd + cost:.2f} > {rk['max_total_exposure_usd']}"))
                if available_usd is not None:
                    checks.append(("balance", cost <= available_usd - rk["balance_reserve_usd"],
                                   f"cost {cost:.2f} > available {available_usd:.2f} - reserve"))
            fee = kalshi_fee(it.price, it.count, it.fee_type, it.fee_mult, maker=it.post_only) / max(it.count, 1) * 100
            if it.forced:
                pass
            elif it.expected_edge_c is not None:
                need = rk.get("min_quote_edge_c", 0.5) if it.tag == "quote" else rk["min_edge_over_fees_c"]
                checks.append(("economics", it.expected_edge_c >= fee + need,
                               f"edge {it.expected_edge_c:.2f}c < fees {fee:.2f}c + {need}c"))
            else:
                checks.append(("economics", False, "no code-side edge estimate"))
            pin = self.state.pinned_version
            if it.model_version is not None and not it.forced:
                checks.append(("model_version", (not pin) or it.model_version in (pin, "rules-v1"),
                               f"judge answered as {it.model_version}, thresholds tuned on {pin}"))
            if it.band and it.tag == "quote":
                lo, hi = it.band
                checks.append(("range_guard", lo <= it.price <= hi, f"price {it.price} left band [{lo}, {hi}]"))
        for name, ok, msg in checks:
            if not ok:
                logs.write("vetoes", {"bot": it.bot, "ticker": it.ticker, "check": name, "message": msg,
                                      "intent": it.to_dict()})
                return False, f"{name}: {msg}"
        return True, "ok"
