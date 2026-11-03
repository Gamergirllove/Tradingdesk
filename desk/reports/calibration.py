"""Calibration on your own data: label every answer with what actually happened, bucket by probability,
Brier score, reliability curve, and a Platt fit that is only proposed (applying it needs APPROVE)."""
from __future__ import annotations

import bisect
import json
import time
from collections import defaultdict

from .. import logs
from ..config import data_dir, load_config
from ..policy.calibration import fit_platt

H_SHORT, H_MED = 5 * 60, 30 * 60


class Mids:
    def __init__(self, days: int = 3):
        self.s: dict[str, list[tuple]] = defaultdict(list)
        for r in logs.read_all("mids", days=days):
            if r.get("mid") is not None:
                self.s[r["ticker"]].append((r["ts"], r["mid"], r.get("bid"), r.get("ask"), r.get("spread"), r.get("v30")))
        for v in self.s.values():
            v.sort()
        self.keys = {t: [x[0] for x in v] for t, v in self.s.items()}

    def at(self, t: str, ts: float, tol: float = 150):
        ks = self.keys.get(t)
        if not ks:
            return None
        i = bisect.bisect_left(ks, ts)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(ks) and abs(ks[j] - ts) <= tol:
                if best is None or abs(ks[j] - ts) < abs(ks[best] - ts):
                    best = j
        return self.s[t][best] if best is not None else None

    def window(self, t: str, a: float, b: float):
        ks = self.keys.get(t) or []
        return self.s[t][bisect.bisect_left(ks, a):bisect.bisect_right(ks, b)] if ks else []


def _either_correct(m0, m30, fee_c: float = 2.0):
    if not m0 or not m30 or m0[2] is None or m0[3] is None:
        return None
    up = (m30[1] - m0[3]) * 100 >= 1 + fee_c / 2
    dn = (m0[2] - m30[1]) * 100 >= 1 + fee_c / 2
    return "buy_yes" if up else ("buy_no" if dn else "hold")


def _label_gambler(d: dict, M: Mids) -> dict:
    t, ts, a, s = d["ticker"], d["ts"], d.get("answers") or {}, d.get("state") or {}
    m0, m15, m30 = M.at(t, ts), M.at(t, ts + 900), M.at(t, ts + H_MED)
    if not m0 or not m30:
        return {}
    out = {}
    d30 = (m30[1] - m0[1]) * 100
    path = M.window(t, ts, ts + H_MED)
    rng = (max(x[1] for x in path) - min(x[1] for x in path)) * 100 if path else 0
    label = "trending" if abs(d30) >= 3 else ("chaotic" if rng >= 6 else "mean_reverting")
    for opt in ("trending", "mean_reverting", "chaotic"):
        p = (a.get("regime") or {}).get("probs", {}).get(opt)
        if p is not None:
            out[f"gambler.regime.{opt}"] = (p, int(label == opt))
    if m15 and (a.get("pressure") or {}).get("p") is not None:
        out["gambler.pressure"] = (a["pressure"]["p"], int((m15[1] - m0[1]) * 100 >= 1))
    act = _either_correct(m0, m30)
    if act:
        for opt in ("buy_yes", "buy_no", "hold"):
            p = (a.get("action") or {}).get("probs", {}).get(opt)
            if p is not None:
                out[f"gambler.action.{opt}"] = (p, int(act == opt))
        if (a.get("worth") or {}).get("p") is not None:
            out["gambler.worth"] = (a["worth"]["p"], int(act != "hold"))
    edge = s.get("model_edge_c")
    ge2 = (a.get("setup") or {}).get("p_ge", {}).get("2")
    if edge and ge2 is not None and m0[4] is not None:
        pnl = (1 if edge > 0 else -1) * d30 - m0[4] * 50 - 1.5
        out["gambler.setup.ge2"] = (ge2, int(pnl > 0))
    w5 = M.window(t, ts, ts + H_SHORT)
    if w5 and m0[4] is not None and (a.get("exec_degraded") or {}).get("p") is not None:
        avg_sp = sum((x[4] or 0) for x in w5) / len(w5)
        out["gambler.exec_degraded"] = (a["exec_degraded"]["p"], int(avg_sp > m0[4] + 0.005))
    return out


def _label_house(d: dict, M: Mids) -> dict:
    t, ts, a, s = d["ticker"], d["ts"], d.get("answers") or {}, d.get("state") or {}
    m0, m30 = M.at(t, ts), M.at(t, ts + H_MED)
    if not m0 or not m30:
        return {}
    out = {}
    d30 = (m30[1] - m0[1]) * 100
    lo, hi = s.get("band_lo"), s.get("band_hi")
    brk = (lo is not None and hi is not None and not (lo <= m30[1] <= hi)) or abs(d30) >= 4
    for opt, y in (("breaking_out", int(brk)), ("inside", int(not brk))):
        p = (a.get("range_state") or {}).get("probs", {}).get(opt)
        if p is not None:
            out[f"house.range_state.{opt}"] = (p, y)
    w5 = M.window(t, ts, ts + H_SHORT)
    if w5 and (a.get("toxic") or {}).get("p") is not None:
        mv = max(abs(x[1] - m0[1]) for x in w5) * 100
        out["house.toxic"] = (a["toxic"]["p"], int(mv >= 2))
    path = M.window(t, ts, ts + H_MED)
    if path and (a.get("env") or {}).get("p_ge", {}).get("2") is not None:
        rng = (max(x[1] for x in path) - min(x[1] for x in path)) * 100
        changes = sum(1 for x, y in zip(path, path[1:]) if x[1] != y[1])
        out["house.env.ge2"] = (a["env"]["p_ge"]["2"], int(rng <= 4 and changes >= 3))
    if (a.get("volume_alive") or {}).get("p") is not None and m0[5] is not None and m30[5] is not None:
        out["house.volume_alive"] = (a["volume_alive"]["p"], int(m30[5] >= max(1, m0[5]) / 3))
    return out


def _label_scan(d: dict, M: Mids) -> dict:
    out = {}
    bot = d["bot"].split(".")[0]
    for cid, t in (d.get("tickers") or {}).items():
        p = (d.get("answers") or {}).get(f"worth_{cid}", {}).get("p")
        if p is None:
            continue
        m0, m30 = M.at(t, d["ts"]), M.at(t, d["ts"] + H_MED)
        if not m0 or not m30:
            continue
        if bot == "gambler":
            act = _either_correct(m0, m30)
            if act:
                out[f"{bot}.scan.worth"] = out.get(f"{bot}.scan.worth", []) + [(p, int(act != "hold"))]
        else:
            path = M.window(t, d["ts"], d["ts"] + H_MED)
            rng = (max(x[1] for x in path) - min(x[1] for x in path)) * 100 if path else 99
            out[f"{bot}.scan.worth"] = out.get(f"{bot}.scan.worth", []) + [(p, int(rng <= 4))]
    return out


def label_outcomes() -> int:
    done_p = data_dir() / "labelled_ids.json"
    done = set(json.loads(done_p.read_text())) if done_p.exists() else set()
    decs = logs.read_all("decisions", days=3)
    M = Mids(days=4)
    n = 0
    cutoff = time.time() - H_MED - 180
    for d in decs:
        did = d.get("decision_id")
        if not did or did in done or d.get("ts", 0) > cutoff or d.get("backend") != "claude":
            continue
        if not d.get("answers"):
            done.add(did)
            continue
        bot = d.get("bot", "")
        if bot == "gambler":
            lab = _label_gambler(d, M)
        elif bot == "house":
            lab = _label_house(d, M)
        elif bot.endswith(".scan"):
            lab = _label_scan(d, M)
        else:
            lab = {}
        done.add(did)
        if lab:
            logs.write("outcomes", {"decision_id": did, "bot": bot, "ticker": d.get("ticker"), "ts_decision": d["ts"],
                                    "model_version": d.get("model_version"), "labels": lab})
            n += 1
    done_p.write_text(json.dumps(sorted(done)[-50000:]))
    return n


def build_report(propose: bool = False, days: int = 14) -> dict:
    cfg = load_config()
    rows = logs.read_all("outcomes", days=days)
    pts: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for r in rows:
        for k, v in (r.get("labels") or {}).items():
            if isinstance(v, list) and v and isinstance(v[0], list):
                pts[k].extend((float(p), int(y)) for p, y in v)
            elif isinstance(v, (list, tuple)) and len(v) == 2:
                pts[k].append((float(v[0]), int(v[1])))
    rep = {"built": time.time(), "keys": {}, "n_labelled": sum(len(v) for v in pts.values())}
    proposed = {}
    for k, v in sorted(pts.items()):
        bins = []
        for i in range(10):
            lo, hi = i / 10, (i + 1) / 10
            b = [(p, y) for p, y in v if lo <= p < hi or (i == 9 and p == 1.0)]
            if b:
                bins.append({"lo": lo, "hi": hi, "n": len(b), "pred": round(sum(p for p, _ in b) / len(b), 4),
                             "freq": round(sum(y for _, y in b) / len(b), 4)})
        brier = round(sum((p - y) ** 2 for p, y in v) / len(v), 4)
        base = sum(y for _, y in v) / len(v)
        brier_ref = round(sum((base - y) ** 2 for _, y in v) / len(v), 4)
        entry = {"n": len(v), "brier": brier, "brier_climatology": brier_ref, "base_rate": round(base, 4), "bins": bins}
        if len(v) >= cfg["calibration"]["min_samples"]:
            a, b0 = fit_platt([p for p, _ in v], [y for _, y in v])
            entry["platt"] = {"a": a, "b": b0}
            proposed[k] = {"a": a, "b": b0, "n": len(v)}
        rep["keys"][k] = entry
    rep["summary"] = {k: {"n": e["n"], "brier": e["brier"], "vs_base": e["brier_climatology"]} for k, e in rep["keys"].items()}
    (data_dir() / "calibration_report.json").write_text(json.dumps(rep))
    if propose and proposed:
        (data_dir() / "calibration_proposed.json").write_text(json.dumps({"built": time.time(), "params": proposed}))
        rep["proposed"] = True
    return rep


def load_report() -> dict | None:
    p = data_dir() / "calibration_report.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None
