"""Funnel stage 4: compare a few candidates, each as a two-line summary.

A Choice is relative and a Noul is absolute, so every "which one" ships with "worth doing at all" Nouls.
Never a Choice over a single option.
"""
from .base import Q, choice, noul


def battery(bot: str, cids: list[str]) -> list[Q]:
    goal = ("a directional entry (one position)" if bot == "gambler"
            else "two-sided market making (resting quotes on both sides)")
    qs: list[Q] = []
    if len(cids) >= 2:
        qs.append(choice("best", f"Which candidate is best suited to {goal}?",
                         {c: f"candidate {c}" for c in cids}, ["candidates"]))
    for c in cids:
        qs.append(noul(f"worth_{c}", f"Candidate {c} is worth {goal} at all right now.", ["candidates"]))
    return qs
