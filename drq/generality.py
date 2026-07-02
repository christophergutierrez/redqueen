"""Generality evaluation — the paper's key progress metric.

The paper defines generality as the fraction of *held-out* human warriors a
warrior defeats/ties. Here: the fraction of a held-out challenge set (schemas +
questions the solver never trained against) that a champion answers correctly.

Rising generality over rounds = the Red Queen effect actually working, rather
than the solver just overfitting the adversary's latest tricks.

Evaluation delegates entirely to domain.score_challenges() so this module has
no knowledge of how any specific domain evaluates its genomes.
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from .llm import LLMClient


def generality(champion_genome: Any, heldout: Sequence, worker: LLMClient,
               domain) -> dict:
    result = domain.score_challenges(champion_genome, list(heldout), worker)
    return {
        "generality": result.get("accuracy", 0.0),
        "n_heldout": result.get("n_challenges", 0),
        "per_tag": result.get("per_tag", {}),
    }


def evaluate_lineage(champions_json: str, heldout: Sequence, worker: LLMClient,
                     domain) -> list[dict]:
    """Score every round's champion against the same held-out set -> generality curve."""
    with open(champions_json) as f:
        champs = json.load(f)
    curve = []
    for c in champs:
        g = generality(c["genome"], heldout, worker, domain)
        curve.append({"round": c["round"], "train_fitness": c["fitness"], **g})
        print(f"[gen] round {c['round']:>2} "
              f"train={c['fitness']:.3f} heldout_generality={g['generality']:.3f}")
    return curve
