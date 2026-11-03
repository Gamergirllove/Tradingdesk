"""One position at a time per bot, and a bench where the rejection reason sets how long a market sits out."""
from __future__ import annotations

import json
import time

from .. import logs
from ..config import data_dir


class Book:
    """While a bot holds a lock, its scan does not run. Only the path that closes the position frees it."""

    def __init__(self):
        self.p = data_dir() / "book.json"
        self.locks: dict[str, dict] = {}
        if self.p.exists():
            try:
                self.locks = json.loads(self.p.read_text())
            except ValueError:
                self.locks = {}

    def _save(self):
        self.p.write_text(json.dumps(self.locks, indent=1))

    def held(self, bot: str) -> dict | None:
        return self.locks.get(bot)

    def acquire(self, bot: str, ticker: str, lead_id: str, info: dict | None = None) -> bool:
        if bot in self.locks:
            return False
        self.locks[bot] = {"ticker": ticker, "lead_id": lead_id, "since": time.time(), **(info or {})}
        self._save()
        logs.write("events", {"kind": "book_acquire", "bot": bot, "ticker": ticker, "lead_id": lead_id})
        return True

    def update(self, bot: str, **kv):
        if bot in self.locks:
            self.locks[bot].update(kv)
            self._save()

    def release(self, bot: str, closed_by: str, lead_id: str):
        lk = self.locks.get(bot)
        if not lk:
            return
        if lk.get("lead_id") != lead_id:
            logs.write("events", {"kind": "book_release_mismatch", "bot": bot, "have": lk.get("lead_id"),
                                  "got": lead_id})
            return
        self.locks.pop(bot, None)
        self._save()
        logs.write("events", {"kind": "book_release", "bot": bot, "ticker": lk.get("ticker"), "by": closed_by,
                              "lead_id": lead_id})


class Bench:
    def __init__(self, ttl_minutes: dict[str, float]):
        self.ttl = ttl_minutes
        self.p = data_dir() / "bench.json"
        self.items: dict[str, dict] = {}
        if self.p.exists():
            try:
                self.items = json.loads(self.p.read_text())
            except ValueError:
                self.items = {}

    def _save(self):
        self.p.write_text(json.dumps(self.items))

    def bench(self, key: str, reason: str, until: float | None = None):
        mins = float(self.ttl.get(reason, 30))
        t = until if until else time.time() + mins * 60
        prev = self.items.get(key)
        if prev and prev["until"] >= t:
            return
        self.items[key] = {"reason": reason, "until": t, "since": time.time()}
        self._save()

    def benched(self, key: str) -> str | None:
        it = self.items.get(key)
        if not it:
            return None
        if time.time() >= it["until"]:
            self.items.pop(key, None)
            self._save()
            return None
        return it["reason"]

    def sweep(self):
        t = time.time()
        dead = [k for k, v in self.items.items() if v["until"] <= t]
        for k in dead:
            self.items.pop(k)
        if dead:
            self._save()
