"""Digital Red Queen driver.

Outer loop (Red Queen): for each round t,
  1. ADVERSARY STEP: evolve a new champion challenge-set that breaks the current
     champion solver -> append to the opponent history {C_0 ... C_{t-1}}.
  2. SOLVER STEP: run MAP-Elites to evolve a solver that maximizes fitness against
     the *entire* opponent history (or last K sets if history_k > 0).
  3. Record the round champion (best solver) + generality metrics.

Inner loop (MAP-Elites): sample elite -> LLM-mutate -> evaluate -> insert.

Evaluation is IO-bound (LLM calls), so candidate batches are scored with a
thread pool. Swap in multiprocessing only if you move to a CPU-bound simulator
(e.g. a real Core War VM) — see notes in README.
"""
from __future__ import annotations

import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .archive import Entity, MapElites
from .budget import TokenBudget
from .config import DRQConfig
from .domains.base import Domain
from .llm import LLMClient


class DRQ:
    def __init__(self, domain: Domain, cfg: DRQConfig):
        self.domain = domain
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        # Shared token ceiling: both roles charge the same budget; the loop halts
        # cleanly when it is reached (see run()).
        self.budget = TokenBudget(cfg.token_budget)
        self.evolver = LLMClient(cfg.evolver_llm or cfg.llm, budget=self.budget)
        self.worker = LLMClient((cfg.worker_llm or cfg.llm).as_worker(), budget=self.budget)
        self.opponents: list[Any] = []   # growing history {C_0..C_{t-1}}
        self.champions: list[Entity] = []  # solver champion per round
        self._halted = False               # set True if a run stops on the token budget
        os.makedirs(cfg.out_dir, exist_ok=True)
        self._log_path = os.path.join(cfg.out_dir, "run.jsonl")

    # ------------------------------------------------------------------ eval
    def _active_opponents(self) -> list[Any]:
        if self.cfg.history_k and self.cfg.history_k > 0:
            return self.opponents[-self.cfg.history_k:]
        return self.opponents  # full history = paper's "full DRQ"

    def _score(self, genome: Any, seed: int) -> Entity:
        f, beh, meta = self.domain.fitness(
            genome, self._active_opponents(), seed, worker_llm=self.worker)
        return Entity(genome=genome, fitness=f, behavior=beh,
                      cell=self.domain.cell(beh), meta=meta)

    def _score_batch(self, genomes: list[Any]) -> list[Entity]:
        with ThreadPoolExecutor(max_workers=self.cfg.eval_workers) as ex:
            return list(ex.map(lambda g: self._score(g, self.rng.randint(0, 1 << 30)), genomes))

    # --------------------------------------------------------- adversary step
    def _adversary_step(self, round_idx: int, champion: Entity | None) -> Any:
        """Evolve a challenge-set targeting the current champion solver."""
        target = champion.genome if champion else self.domain.new_genome(self.evolver)
        n_want = self.cfg.challenges_per_round
        challenges: list = []
        tries = 0
        while len(challenges) < n_want and tries < n_want * 4 and not self.budget.exceeded():
            ch = self.domain.new_challenge(self.evolver, target)
            tries += 1
            if ch is not None:
                challenges.append(ch)
        # round 0 falls back to seeds if the adversary produced nothing usable
        if not challenges:
            challenges = list(getattr(self.domain, "seed_challenges", []))
        return self.domain.wrap_opponent(round_idx, challenges)

    # ------------------------------------------------------------ solver step
    def _solver_step(self) -> MapElites:
        me = MapElites(self.rng)
        mecfg = self.cfg.me

        # seed archive with prior champions (paper bootstraps like this)
        if mecfg.seed_with_champions and self.champions:
            for champ in self._score_batch([c.genome for c in self.champions]):
                me.add(champ)

        # fresh random init
        inits = [self.domain.new_genome(self.evolver) for _ in range(mecfg.init_random)]
        for e in self._score_batch(inits):
            me.add(e)

        # inner MAP-Elites iterations
        for _ in range(mecfg.iterations):
            if self.budget.exceeded():
                break   # stop spawning work; the outer loop will halt the run
            parents = [me.sample() for _ in range(mecfg.batch_size)]
            children = [self.domain.mutate(self.evolver, p.genome)
                        for p in parents if p is not None]
            for e in self._score_batch(children):
                me.add(e)
        return me

    # ------------------------------------------------------------------- loop
    def run(self) -> list[Entity]:
        champion: Entity | None = None
        for t in range(self.cfg.rounds):
            if self.budget.exceeded():
                self._halted = True
                snap = self.budget.snapshot()
                print(f"[budget] token ceiling {snap['limit']} reached "
                      f"({snap['tokens']} tokens over {snap['calls']} calls) "
                      f"after {t} rounds — halting cleanly.")
                break
            t0 = time.time()
            # 1. adversary evolves a new opponent challenge-set vs current champion
            cs = self._adversary_step(t, champion)
            self.opponents.append(cs)
            # 2. solver population evolves against the full opponent history
            me = self._solver_step()
            champion = me.best()
            self.champions.append(champion)
            # collect where this round's wall-clock went (LLM vs verify); optional hook
            timing: dict = getattr(self.domain, "pop_timing", lambda: {})()
            # 3. record
            rec = {
                "round": t,
                "elapsed_s": round(time.time() - t0, 1),
                "timing": timing,
                "budget": self.budget.snapshot(),
                "n_opponents": len(self.opponents),
                "n_challenges_total": sum(len(o.challenges) for o in self._active_opponents()),
                "archive_coverage": me.coverage(),
                "qd_score": round(me.qd_score(), 3),
                "champion_fitness": round(champion.fitness, 3) if champion else None,
                "champion_cell": list(champion.cell) if champion else None,
                "champion_meta": champion.meta if champion else None,
                "champion_genome": champion.genome if champion else None,
                "new_challenge_tags": [c.tags for c in cs.challenges],
            }
            self._append_log(rec)
            tinfo = (f" llm={timing['llm_s']}s vrf={timing['verify_s']}s"
                     if timing else "")
            print(f"[round {t:>2}] fit={rec['champion_fitness']} "
                  f"cov={rec['archive_coverage']} qd={rec['qd_score']} "
                  f"opp={rec['n_opponents']} ({rec['elapsed_s']}s){tinfo}")
        self._dump_final()
        return self.champions

    # ------------------------------------------------------------------ io
    def _append_log(self, rec: dict) -> None:
        with open(self._log_path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def _dump_final(self) -> None:
        with open(os.path.join(self.cfg.out_dir, "champions.json"), "w") as f:
            json.dump([{"round": i, "fitness": c.fitness, "genome": c.genome,
                        "cell": list(c.cell)} for i, c in enumerate(self.champions)],
                      f, indent=2)
        with open(os.path.join(self.cfg.out_dir, "opponents.json"), "w") as f:
            json.dump([o.to_dict() for o in self.opponents], f, indent=2)
