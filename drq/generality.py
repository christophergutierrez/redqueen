"""Generality evaluation — the paper's key progress metric.

The paper defines generality as the fraction of *held-out* human warriors a
warrior defeats/ties. Here: the fraction of a held-out challenge set (schemas +
questions the solver never trained against) that a champion answers correctly.

Rising generality over rounds = the Red Queen effect actually working, rather
than the solver just overfitting the adversary's latest tricks.
"""
from __future__ import annotations

import json
from typing import Sequence

from .domains.text2sql import Challenge, Text2SQLDomain, exec_match, extract_sql
from .llm import LLMClient


def generality(domain: Text2SQLDomain, champion_genome: str,
               heldout: Sequence[Challenge], worker: LLMClient) -> dict:
    correct = 0
    per_tag: dict[str, list[int]] = {}
    for ch in heldout:
        user = (f"Schema:\n{ch.schema_sql}\n\nQuestion: {ch.question}\n\n"
                "Return ONLY the DuckDB SQL query.")
        res = worker.chat(system=champion_genome, user=user,
                          temperature=worker.cfg.worker_temperature)
        hit = exec_match(ch.schema_sql, ch.gold_sql, extract_sql(res.text))
        correct += int(hit)
        for t in (ch.tags or ["untagged"]):
            per_tag.setdefault(t, []).append(int(hit))
    return {
        "generality": correct / len(heldout) if heldout else 0.0,
        "n_heldout": len(heldout),
        "per_tag": {t: sum(v) / len(v) for t, v in per_tag.items()},
    }


def evaluate_lineage(domain: Text2SQLDomain, champions_json: str,
                     heldout: Sequence[Challenge], worker: LLMClient) -> list[dict]:
    """Score every round's champion against the same held-out set -> generality curve."""
    with open(champions_json) as f:
        champs = json.load(f)
    curve = []
    for c in champs:
        g = generality(domain, c["genome"], heldout, worker)
        curve.append({"round": c["round"], "train_fitness": c["fitness"], **g})
        print(f"[gen] round {c['round']:>2} "
              f"train={c['fitness']:.3f} heldout_generality={g['generality']:.3f}")
    return curve
