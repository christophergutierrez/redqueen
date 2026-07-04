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
from dataclasses import dataclass
from typing import Any

from .archive import Entity, MapElites
from .budget import TokenBudget
from .config import DRQConfig
from .domains.base import Domain
from .llm import LLMClient


@dataclass
class _DefaultOpponent:
    """Engine-owned Opponent used when a domain doesn't define `wrap_opponent`.
    Satisfies the `Opponent` protocol (round, challenges, to_dict) so the engine's
    reads never crash on a domain that relies on the default."""
    round: int
    challenges: list

    def to_dict(self) -> dict:
        return {"round": self.round,
                "challenges": [c.to_dict() if hasattr(c, "to_dict") else c
                               for c in self.challenges]}


class DRQ:
    def __init__(self, domain: Domain, cfg: DRQConfig):
        self.domain = domain
        self.cfg = cfg
        self.rng = random.Random(cfg.seed)
        # Shared token ceiling: both roles charge the same budget; the loop halts
        # cleanly when it is reached (see run()).
        self.budget = TokenBudget(cfg.token_budget)
        # optional per-domain faithful mock (offline runs); llm.py stays domain-agnostic
        mock_reply = getattr(self.domain, "mock_reply", None)
        self.evolver = LLMClient(cfg.evolver_llm or cfg.llm, budget=self.budget,
                                 role="evolver", mock_reply=mock_reply)
        self.worker = LLMClient((cfg.worker_llm or cfg.llm).as_worker(), budget=self.budget,
                                role="worker", mock_reply=mock_reply)
        # Resolve OPTIONAL domain hooks once, here — the single place that supplies
        # defaults for what a domain may omit (see domains/base.py). No inheritance.
        self._coevolutionary = bool(getattr(self.domain, "is_coevolutionary", lambda: False)())
        self._new_challenge = getattr(self.domain, "new_challenge", lambda llm, target: None)
        self._wrap_opponent = getattr(self.domain, "wrap_opponent", self._default_wrap_opponent)
        self._summarize = getattr(self.domain, "summarize_opponent", self._default_summarize)
        self._genome_to_json = getattr(self.domain, "genome_to_json", lambda g: g)
        self._pop_timing: Any = getattr(self.domain, "pop_timing", lambda: {})
        self.opponents: list[Any] = []   # growing history {C_0..C_{t-1}}
        self.champions: list[Entity] = []  # solver champion per round
        self._halted = False               # set True if a run stops on the token budget
        os.makedirs(cfg.out_dir, exist_ok=True)
        self._log_path = os.path.join(cfg.out_dir, "run.jsonl")

    # --------------------------------------------------- optional-hook defaults
    def _default_wrap_opponent(self, round_idx: int, challenges: list) -> _DefaultOpponent:
        return _DefaultOpponent(round_idx, list(challenges))

    @staticmethod
    def _default_summarize(opponent: Any) -> list:
        """Per-round opponent tag summary for the log (decouples the engine from
        assuming challenges carry `.tags`)."""
        return [getattr(c, "tags", []) for c in opponent.challenges]

    # ------------------------------------------------------------------ eval
    def _active_opponents(self) -> list[Any]:
        if self.cfg.history_k and self.cfg.history_k > 0:
            return self.opponents[-self.cfg.history_k:]
        return self.opponents  # full history = paper's "full DRQ"

    def _score(self, genome: Any, seed: int) -> Entity:
        # One bad candidate must never abort the whole round/run: a scoring
        # failure is treated as a miss (0.0), landed in its own cell, so the
        # batch and all prior rounds' work survive.
        try:
            f, beh, meta = self.domain.fitness(
                genome, self._active_opponents(), seed, worker_llm=self.worker)
            return Entity(genome=genome, fitness=f, behavior=beh,
                          cell=self.domain.cell(beh), meta=meta)
        except Exception as e:  # noqa: BLE001
            try:
                beh = self.domain.behavior(genome, {})
                cell = self.domain.cell(beh)
            except Exception:  # noqa: BLE001
                beh, cell = (), (0,)
            return Entity(genome=genome, fitness=0.0, behavior=beh,
                          cell=cell, meta={"error": str(e)})

    def _score_batch(self, genomes: list[Any]) -> list[Entity]:
        # Generate seeds in the main thread — self.rng is not thread-safe and
        # would race if called inside the lambda from worker threads.
        seeds = [self.rng.randint(0, 1 << 30) for _ in genomes]
        with ThreadPoolExecutor(max_workers=self.cfg.eval_workers) as ex:
            return list(ex.map(self._score, genomes, seeds))

    # --------------------------------------------------------- adversary step
    def _adversary_step(self, round_idx: int, champion: Entity | None) -> Any:
        """Evolve a challenge-set targeting the current champion solver."""
        target = champion.genome if champion else self.domain.new_genome(self.evolver)
        n_want = self.cfg.challenges_per_round
        challenges: list = []
        tries = 0
        while len(challenges) < n_want and tries < n_want * 4 and not self.budget.exceeded():
            ch = self._new_challenge(self.evolver, target)
            tries += 1
            if ch is not None:
                challenges.append(ch)
        # round 0 falls back to seeds if the adversary produced nothing usable
        if not challenges:
            challenges = list(getattr(self.domain, "seed_challenges", []))
        return self._wrap_opponent(round_idx, challenges)

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
            # 1. build this round's opponent. Coevolutionary domains evolve a fresh
            #    challenge-set each round; non-coevolutionary domains install their
            #    fixed seed set ONCE (round 0) instead of duplicating it every round.
            if self._coevolutionary:
                cs = self._adversary_step(t, champion)
                self.opponents.append(cs)
            elif t == 0:
                cs = self._wrap_opponent(0, list(getattr(self.domain, "seed_challenges", [])))
                self.opponents.append(cs)
            else:
                cs = None
            # 2. solver population evolves against the full opponent history
            me = self._solver_step()
            champion = me.best()
            # me.best() is None when the archive ended empty (e.g. --init-random 0
            # with no prior champions); don't store/dereference a None champion.
            if champion is not None:
                self.champions.append(champion)
            # collect where this round's wall-clock went (LLM vs verify); optional hook
            timing: dict = self._pop_timing()
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
                "champion_genome": self._genome_to_json(champion.genome) if champion else None,
                "new_challenge_tags": self._summarize(cs) if cs is not None else None,
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
            json.dump([{"round": i, "fitness": c.fitness,
                        "genome": self._genome_to_json(c.genome),
                        "cell": list(c.cell)} for i, c in enumerate(self.champions)],
                      f, indent=2)
        with open(os.path.join(self.cfg.out_dir, "opponents.json"), "w") as f:
            json.dump([o.to_dict() for o in self.opponents], f, indent=2)
