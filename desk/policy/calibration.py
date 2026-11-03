"""Platt scaling in the policy layer. Identity until enough labelled answers exist and the fit is approved."""
from __future__ import annotations

import json
import math

from ..config import data_dir


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


class Calibrator:
    def __init__(self):
        self.params: dict[str, tuple[float, float]] = {}
        self.load()

    def path(self):
        return data_dir() / "calibration_applied.json"

    def load(self):
        p = self.path()
        if p.exists():
            try:
                raw = json.loads(p.read_text())
                self.params = {k: (float(v["a"]), float(v["b"])) for k, v in raw.get("params", {}).items()}
            except (ValueError, KeyError, TypeError):
                self.params = {}

    def __call__(self, key: str, p: float) -> float:
        ab = self.params.get(key)
        if not ab:
            return p
        a, b = ab
        return 1 / (1 + math.exp(-(a * _logit(p) + b)))


def fit_platt(ps: list[float], ys: list[int], iters: int = 300, lr: float = 0.1) -> tuple[float, float]:
    """Logistic regression of outcome on logit(p). Plain gradient descent, no deps."""
    a, b = 1.0, 0.0
    xs = [_logit(p) for p in ps]
    n = len(xs)
    for _ in range(iters):
        ga = gb = 0.0
        for x, y in zip(xs, ys):
            q = 1 / (1 + math.exp(-(a * x + b)))
            ga += (q - y) * x
            gb += (q - y)
        a -= lr * ga / n
        b -= lr * gb / n
    return round(a, 5), round(b, 5)
