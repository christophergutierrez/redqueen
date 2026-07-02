#!/usr/bin/env python3
"""DRQ CLI.

  python run.py evolve --rounds 12 --out runs/sql1
  python run.py generality --champions runs/sql1/champions.json --out runs/sql1
  DRQ_LLM_MOCK=1 python run.py evolve --rounds 4 --iterations 6   # offline smoke test

Point at a local model with env vars:
  OPENAI_BASE_URL=http://localhost:8000/v1  DRQ_MODEL=Qwen/Qwen2.5-Coder-32B-Instruct  (vLLM)
  OPENAI_BASE_URL=http://localhost:11434/v1 DRQ_MODEL=qwen2.5-coder:32b                (Ollama)
"""
from __future__ import annotations

import argparse
import json
import os

from drq.config import DRQConfig, LLMConfig, MapElitesConfig
from drq.domains.text2sql import Challenge, Text2SQLDomain
from drq.engine import DRQ
from drq.generality import evaluate_lineage
from drq.llm import LLMClient


HELDOUT = [
    Challenge(
        schema_sql=("CREATE TABLE t(id INTEGER, cat TEXT, v INTEGER, ts DATE);"
                    "INSERT INTO t VALUES (1,'x',10,'2024-01-01'),(2,'x',20,'2024-02-01'),"
                    "(3,'y',5,'2024-01-15'),(4,'y',NULL,'2024-03-01');"),
        question="Average v per cat, ignoring NULLs, only cats whose average exceeds 8.",
        gold_sql=("SELECT cat, AVG(v) a FROM t WHERE v IS NOT NULL GROUP BY cat "
                  "HAVING AVG(v)>8 ORDER BY cat;"),
        tags=["group_by", "having", "null"],
    ),
    Challenge(
        schema_sql=("CREATE TABLE a(id INTEGER, name TEXT);CREATE TABLE b(a_id INTEGER, tag TEXT);"
                    "INSERT INTO a VALUES (1,'p'),(2,'q'),(3,'r');"
                    "INSERT INTO b VALUES (1,'red'),(1,'blue'),(2,'red');"),
        question="Names in a with no matching row in b.",
        gold_sql=("SELECT name FROM a WHERE id NOT IN (SELECT a_id FROM b) ORDER BY name;"),
        tags=["anti_join", "subquery"],
    ),
]


def cmd_evolve(args):
    evolver_llm = LLMConfig(model=args.evolver_model) if args.evolver_model else None
    worker_llm = LLMConfig(model=args.worker_model, temperature=0.0) if args.worker_model else None
    cfg = DRQConfig(
        rounds=args.rounds,
        history_k=args.history_k,
        out_dir=args.out,
        eval_workers=args.workers,
        seed=args.seed,
        challenges_per_round=args.challenges_per_round,
        me=MapElitesConfig(iterations=args.iterations,
                           init_random=args.init_random,
                           batch_size=args.batch),
        evolver_llm=evolver_llm,
        worker_llm=worker_llm,
    )
    domain = Text2SQLDomain()
    DRQ(domain, cfg).run()
    print(f"\nDone. Champions -> {os.path.join(args.out, 'champions.json')}")


def cmd_generality(args):
    if args.heldout:
        with open(args.heldout) as f:
            raw = json.load(f)
        heldout = [Challenge(**c) for c in raw]
    else:
        heldout = HELDOUT
    domain = Text2SQLDomain()
    worker = LLMClient(DRQConfig().llm)
    curve = evaluate_lineage(args.champions, heldout, worker, domain)
    out = os.path.join(args.out, "generality.json")
    with open(out, "w") as f:
        json.dump(curve, f, indent=2)
    print(f"\nGenerality curve -> {out}")


def main():
    p = argparse.ArgumentParser(description="Digital Red Queen (DRQ) — Text2SQL port")
    sub = p.add_subparsers(required=True)

    e = sub.add_parser("evolve")
    e.add_argument("--rounds", type=int, default=12)
    e.add_argument("--history-k", type=int, default=0, help="0 = full history")
    e.add_argument("--iterations", type=int, default=40)
    e.add_argument("--init-random", type=int, default=8)
    e.add_argument("--batch", type=int, default=4)
    e.add_argument("--workers", type=int, default=8)
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--challenges-per-round", type=int, default=3,
                   help="target number of adversary challenges per round")
    e.add_argument("--evolver-model", default=None, metavar="MODEL",
                   help="override model for the evolver LLM (default: uses DRQ_MODEL env var)")
    e.add_argument("--worker-model", default=None, metavar="MODEL",
                   help="override model for the worker LLM (default: uses DRQ_MODEL env var)")
    e.add_argument("--out", default="runs/default")
    e.set_defaults(func=cmd_evolve)

    g = sub.add_parser("generality")
    g.add_argument("--champions", required=True)
    g.add_argument("--out", default="runs/default")
    g.add_argument("--heldout", default=None, metavar="PATH",
                   help="JSON file with held-out challenges; defaults to builtin set")
    g.set_defaults(func=cmd_generality)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
