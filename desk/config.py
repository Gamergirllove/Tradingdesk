"""Config loading. Base file + approved overrides in the data dir."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
LADDER = ["shadow", "paper", "demo", "live"]


def data_dir() -> Path:
    d = Path(os.environ.get("DESK_DATA", ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def secrets_dir() -> Path:
    d = Path(os.environ.get("DESK_SECRETS", data_dir() / "secrets"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def overrides_path() -> Path:
    return data_dir() / "overrides.yaml"


def load_config() -> dict:
    path = Path(os.environ.get("DESK_CONFIG", ROOT / "config" / "desk.yaml"))
    base = yaml.safe_load(path.read_text())
    op = overrides_path()
    if op.exists():
        base = _merge(base, yaml.safe_load(op.read_text()) or {})
    return base


def get_path(cfg: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_override(dotted: str, value: Any) -> None:
    """Write one approved override. Only called from the approval path."""
    op = overrides_path()
    cur = yaml.safe_load(op.read_text()) if op.exists() else {}
    cur = cur or {}
    node = cur
    parts = dotted.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    tmp = op.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(cur, sort_keys=False))
    tmp.replace(op)


def effective_mode(cfg: dict, bot: str) -> str:
    want = get_path(cfg, f"bots.{bot}.mode", "shadow")
    ceiling = get_path(cfg, "desk.mode_ceiling", "shadow")
    if want not in LADDER:
        want = "shadow"
    if ceiling not in LADDER:
        ceiling = "shadow"
    return LADDER[min(LADDER.index(want), LADDER.index(ceiling))]


def series_of(ticker: str | None) -> str:
    return (ticker or "").split("-")[0]


def profile(cfg: dict, section: str, series: str | None) -> dict:
    """Base settings for a section ('funnel', 'gambler', 'house') with per-series overrides on top."""
    if section == "funnel":
        base = cfg.get("funnel", {})
    else:
        base = cfg.get("bots", {}).get(section, {})
    over = (cfg.get("series_profiles", {}).get(series or "", {}) or {}).get(section, {}) or {}
    return {**base, **over}
