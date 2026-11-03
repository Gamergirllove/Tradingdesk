"""Append-only JSONL logs. Every snapshot, question, answer, decision, order, fill, veto and outcome."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Iterator

from .config import data_dir
from .util import local_day, now

_lock = threading.Lock()
SECRET_KEYS = {"private_key", "api_key", "anthropic_key", "kalshi_pem", "password", "secret", "pem"}


def _scrub(obj):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if any(s in k.lower() for s in SECRET_KEYS) else _scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def log_dir(day: str | None = None) -> Path:
    d = data_dir() / "logs" / (day or local_day())
    d.mkdir(parents=True, exist_ok=True)
    return d


def write(stream: str, record: dict) -> None:
    rec = {"ts": round(now(), 3), **_scrub(record)}
    line = json.dumps(rec, separators=(",", ":"), default=str)
    path = log_dir() / f"{stream}.jsonl"
    with _lock:
        with open(path, "a") as f:
            f.write(line + "\n")
        try:
            os.chmod(path, 0o660)
        except OSError:
            pass


def read(stream: str, day: str | None = None, limit: int | None = None) -> list[dict]:
    path = log_dir(day) / f"{stream}.jsonl"
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    if limit:
        lines = lines[-limit:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def iter_days() -> Iterator[str]:
    base = data_dir() / "logs"
    if not base.exists():
        return iter(())
    return iter(sorted(p.name for p in base.iterdir() if p.is_dir()))


def read_all(stream: str, days: int = 14) -> list[dict]:
    out: list[dict] = []
    for d in list(iter_days())[-days:]:
        out.extend(read(stream, d))
    return out
