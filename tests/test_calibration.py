import json
import time

from desk import logs
from desk.reports import calibration as calrep
from desk.util import local_day


def _write(stream, rows):
    p = logs.log_dir(local_day()) / f"{stream}.jsonl"
    with open(p, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_label_and_report(tmp_data):
    now = time.time()
    t0 = now - 45 * 60
    mids = [{"ts": t0 + i * 60, "ticker": "T", "mid": round(0.40 + 0.002 * i, 4), "bid": round(0.395 + 0.002 * i, 4),
             "ask": round(0.405 + 0.002 * i, 4), "spread": 0.01, "v30": 300} for i in range(46)]
    _write("mids", mids)
    ans = {"regime": {"probs": {"trending": 0.8, "mean_reverting": 0.1, "chaotic": 0.05, "other": 0.05}},
           "pressure": {"p": 0.7},
           "setup": {"p_ge": {"2": 0.7}},
           "action": {"probs": {"buy_yes": 0.7, "buy_no": 0.05, "hold": 0.2, "other": 0.05}},
           "worth": {"p": 0.7}, "exec_degraded": {"p": 0.2}}
    decs = [{"ts": t0 + 60 * k, "bot": "gambler", "decision_id": f"d{k}", "ticker": "T", "backend": "claude",
             "answers": ans, "state": {"model_edge_c": 4}} for k in range(5)]
    decs.append({"ts": t0, "bot": "gambler", "decision_id": "mock1", "ticker": "T", "backend": "mock", "answers": ans})
    _write("decisions", decs)
    n = calrep.label_outcomes()
    assert n == 5                                       # mock answers never calibrate
    assert calrep.label_outcomes() == 0                 # labelled once
    out = logs.read("outcomes")
    lab = out[0]["labels"]
    assert lab["gambler.regime.trending"][1] == 1       # +6c in 30 min = trending
    assert lab["gambler.action.buy_yes"][1] == 1
    assert lab["gambler.pressure"][1] == 1
    rep = calrep.build_report()
    k = rep["keys"]["gambler.regime.trending"]
    assert k["n"] == 5 and k["bins"][0]["freq"] == 1.0 and abs(k["brier"] - 0.04) < 1e-6
