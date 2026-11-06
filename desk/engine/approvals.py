"""The approval line. Anything that changes a risk limit, raises a bot's rung, resumes after a halt or changes
what the thresholds were tuned on waits for the literal word APPROVE. Not a summary, not a paraphrase."""
from __future__ import annotations

import json
import shutil
import time

from .. import logs, secrets
from ..config import LADDER, data_dir, set_override
from ..util import new_id

ALLOWED_PREFIXES = ("risk.", "policy.", "funnel.", "bench_ttl_minutes.", "judge.daily_budget_usd", "judge.backend",
                    "judge.mode", "bots.gambler.", "bots.house.", "desk.mode_ceiling", "venue.universe_series",
                    "seats.")
SIZE_KEYS = ("contracts", "quote_contracts", "max_inventory", "max_order_contracts", "max_position_contracts",
             "max_total_exposure_usd", "daily_loss_limit_usd")


def _category(kind: str, payload: dict) -> str:
    if kind == "mode_change":
        return "size"
    path = payload.get("path", "")
    if any(path.endswith(k) for k in SIZE_KEYS):
        return "size"
    if path.startswith(("policy.", "funnel.", "bots.")) or kind == "apply_calibration":
        return "strategy"
    return "other"


class Approvals:
    def __init__(self, desk):
        self.d = desk
        self.path = data_dir() / "approvals.json"
        self.items: list[dict] = []
        if self.path.exists():
            try:
                self.items = json.loads(self.path.read_text())
            except ValueError:
                self.items = []

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items[-300:], indent=1))
        tmp.replace(self.path)

    def pending(self) -> list[dict]:
        return [i for i in self.items if i["status"] == "pending"]

    def _week_warning(self, cat: str) -> str:
        other = {"size": "strategy", "strategy": "size"}.get(cat)
        if not other:
            return ""
        week = time.time() - 7 * 86400
        for i in self.items:
            if i["status"] == "approved" and i.get("decided", 0) >= week and i.get("category") == other:
                return (f" Warning: a {other} change was approved {time.strftime('%b %d', time.localtime(i['decided']))}. "
                        f"Changing size and strategy in the same week hides which one broke something.")
        return ""

    def request(self, kind: str, title: str, detail: str, payload: dict, source: str = "human") -> dict:
        for i in self.pending():
            if i["kind"] == kind and i["payload"] == payload:
                return i
        if kind == "config_change" and not str(payload.get("path", "")).startswith(ALLOWED_PREFIXES):
            raise ValueError(f"path {payload.get('path')} is not changeable from the desk")
        cat = _category(kind, payload)
        item = {"id": new_id("ap_"), "kind": kind, "title": title, "detail": detail + self._week_warning(cat),
                "payload": payload, "source": source, "status": "pending", "created": time.time(), "category": cat}
        self.items.append(item)
        self._save()
        logs.write("events", {"kind": "approval_requested", "approval": item})
        return item

    async def decide(self, aid: str, text: str, approve: bool = True) -> tuple[bool, str]:
        item = next((i for i in self.items if i["id"] == aid), None)
        if not item or item["status"] != "pending":
            return False, "not pending"
        if not approve:
            item.update(status="rejected", decided=time.time())
            self._save()
            logs.write("events", {"kind": "approval_rejected", "id": aid})
            return True, "rejected"
        if text != "APPROVE":
            return False, "Type APPROVE exactly (all caps). Nothing was changed."
        ok, msg = self._apply(item)
        item.update(status="approved" if ok else "failed", decided=time.time(), result=msg)
        self._save()
        logs.write("events", {"kind": "approval_applied" if ok else "approval_failed", "id": aid, "result": msg})
        return ok, msg

    def _apply(self, item: dict) -> tuple[bool, str]:
        k, p = item["kind"], item["payload"]
        d = self.d
        if k == "mode_change":
            bot, to = p["bot"], p["to"]
            if to not in LADDER:
                return False, "bad rung"
            if to == "demo" and not secrets.exists("kalshi_demo_key.pem"):
                return False, "add a Kalshi DEMO API key first"
            if to == "live" and not secrets.exists("kalshi_key.pem"):
                return False, "add a Kalshi API key first"
            set_override(f"bots.{bot}.mode", to)
            d.reload()
            return True, f"{bot} now runs at {to}"
        if k == "judge_switch":
            set_override("judge.backend", p["to"])
            d.risk.pin("")
            d.reload()
            return True, f"judge is now {p['to']}"
        if k == "resume":
            lim = d.resume_after_loss(p["bot"]) if p.get("bot") else None
            d.risk.clear_halt(p.get("bot"))
            return True, "resumed" if lim is None else f"resumed: stops if today's loss reaches ${lim:.2f}"
        if k == "config_change":
            if p["path"] == "judge.backend" and p["value"] not in ("rules", "claude"):
                return False, "judge must be rules or claude"
            set_override(p["path"], p["value"])
            if p["path"] == "judge.backend":
                d.risk.pin("")
            d.reload()
            return True, f"{p['path']} = {p['value']}"
        if k == "repin":
            d.risk.pin(p["version"])
            return True, f"pinned {p['version']}"
        if k == "apply_calibration":
            src = data_dir() / "calibration_proposed.json"
            if not src.exists():
                return False, "no proposed calibration"
            shutil.copy(src, data_dir() / "calibration_applied.json")
            d.cal.load()
            return True, "calibration applied"
        return False, f"unknown kind {k}"
