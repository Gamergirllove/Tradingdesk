"""Shared AI spend ledger (judge + seats). One cap for the whole desk."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from .config import data_dir
from .util import local_day, now


def _path() -> Path:
    return data_dir() / "ai_ledger.jsonl"


def record(source: str, cost_usd: float, meta: dict | None = None, tz: str = "America/Los_Angeles") -> None:
    rec = {"ts": round(now(), 3), "day": local_day(tz), "source": source, "cost_usd": round(float(cost_usd), 6)}
    if meta:
        rec.update(meta)
    p = _path()
    with open(p, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(rec) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)
    try:
        os.chmod(p, 0o664)
    except OSError:
        pass


def spent(day: str | None = None, tz: str = "America/Los_Angeles") -> float:
    day = day or local_day(tz)
    p = _path()
    if not p.exists():
        return 0.0
    total = 0.0
    with open(p) as f:
        for ln in f:
            if f'"day": "{day}"' not in ln:
                continue
            try:
                total += float(json.loads(ln).get("cost_usd", 0))
            except (ValueError, json.JSONDecodeError):
                pass
    return round(total, 6)


def by_source(day: str | None = None, tz: str = "America/Los_Angeles") -> dict[str, float]:
    day = day or local_day(tz)
    p = _path()
    out: dict[str, float] = {}
    if not p.exists():
        return out
    with open(p) as f:
        for ln in f:
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if rec.get("day") == day:
                out[rec["source"]] = round(out.get(rec["source"], 0.0) + float(rec.get("cost_usd", 0)), 6)
    return out
