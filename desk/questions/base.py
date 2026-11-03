"""Three question types. Noul -> an if. Choice -> a switch. Score -> a sort key.

One judgment per question. Every Choice has an "other" option. Questions never read each other.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

KINDS = ("noul", "choice", "score")


@dataclass
class Q:
    id: str
    kind: str
    text: str
    options: list[str] = field(default_factory=list)            # choice
    option_help: dict[str, str] = field(default_factory=dict)   # choice
    levels: dict[str, str] = field(default_factory=dict)        # score: {"0": rubric, ...}
    reads: list[str] = field(default_factory=list)              # state fields this question reads

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Q":
        return Q(**{k: d[k] for k in Q.__dataclass_fields__ if k in d})


def noul(id: str, text: str, reads: list[str]) -> Q:
    return Q(id=id, kind="noul", text=text, reads=reads)


def choice(id: str, text: str, options: dict[str, str], reads: list[str]) -> Q:
    opts = list(options)
    if "other" not in opts:
        options = {**options, "other": "none of the above fits"}
        opts.append("other")
    return Q(id=id, kind="choice", text=text, options=opts, option_help=dict(options), reads=reads)


def score(id: str, text: str, levels: dict[int, str], reads: list[str]) -> Q:
    return Q(id=id, kind="score", text=text, levels={str(k): v for k, v in sorted(levels.items())}, reads=reads)


def validate(qs: list[Q]) -> list[str]:
    errs = []
    ids = [q.id for q in qs]
    if len(set(ids)) != len(ids):
        errs.append("duplicate question ids")
    if not qs:
        errs.append("empty battery")
    for q in qs:
        if not q.id or not q.id.replace("_", "").isalnum():
            errs.append(f"{q.id}: bad id")
        if q.kind not in KINDS:
            errs.append(f"{q.id}: bad kind {q.kind}")
        if not q.text or len(q.text) > 700:
            errs.append(f"{q.id}: text empty or too long")
        if q.kind == "choice":
            if not (2 <= len(q.options) <= 255):
                errs.append(f"{q.id}: choice needs 2..255 options")
            if "other" not in q.options:
                errs.append(f"{q.id}: choice must include 'other'")
            if len(q.options) - 1 < 2:
                errs.append(f"{q.id}: a choice over a single real option proves nothing")
            if len(set(q.options)) != len(q.options):
                errs.append(f"{q.id}: duplicate options")
        if q.kind == "score":
            keys = sorted(int(k) for k in q.levels)
            if not (2 <= len(keys) <= 10) or keys != list(range(len(keys))):
                errs.append(f"{q.id}: score needs 2..10 contiguous levels from 0")
    return errs


def battery_id(qs: list[Q]) -> str:
    blob = json.dumps([q.to_dict() for q in qs], sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def _num() -> dict:
    return {"type": "number"}


def q_schema(q: Q) -> dict:
    if q.kind == "noul":
        props = {"p_true": _num()}
    elif q.kind == "choice":
        props = {o: _num() for o in q.options}
    else:
        props = {f"L{k}": _num() for k in q.levels}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def schema_for(qs: list[Q]) -> dict:
    props = {q.id: q_schema(q) for q in qs}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _clip(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v != v:
        return 0.0
    return max(0.0, min(1.0, v))


def normalize(q: Q, raw: dict | None) -> dict:
    raw = raw or {}
    if q.kind == "noul":
        return {"p": round(_clip(raw.get("p_true")), 4)}
    if q.kind == "choice":
        vals = {o: _clip(raw.get(o)) for o in q.options}
    else:
        vals = {k: _clip(raw.get(f"L{k}")) for k in q.levels}
    tot = sum(vals.values())
    if tot <= 0:
        probs = {k: round(1.0 / len(vals), 4) for k in vals}
    else:
        probs = {k: round(v / tot, 4) for k, v in vals.items()}
    pick = max(probs, key=probs.get)
    out = {"probs": probs, "pick": pick, "p_pick": probs[pick]}
    if q.kind == "score":
        ks = sorted(int(k) for k in probs)
        out["ev"] = round(sum(int(k) * p for k, p in probs.items()), 4)
        out["p_ge"] = {str(k): round(sum(probs[str(j)] for j in ks if j >= k), 4) for k in ks}
    return out


def render_for_prompt(qs: list[Q]) -> str:
    lines = []
    for q in qs:
        if q.kind == "noul":
            lines.append(f'- id "{q.id}" [NOUL]: {q.text}\n  Answer p_true = probability the statement is true.')
        elif q.kind == "choice":
            opts = "; ".join(f'"{o}" = {q.option_help.get(o, o)}' for o in q.options)
            lines.append(f'- id "{q.id}" [CHOICE]: {q.text}\n  Options: {opts}\n  Give a probability per option.')
        else:
            lv = "; ".join(f'L{k} = {v}' for k, v in q.levels.items())
            lines.append(f'- id "{q.id}" [SCORE]: {q.text}\n  Levels: {lv}\n  Give a probability per level.')
    return "\n".join(lines)
