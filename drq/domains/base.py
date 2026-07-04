"""Domain interface.

A DRQ domain must know how to:
  - describe itself to the LLM (system prompt)
  - generate a fresh entity genome and mutate an existing one (LLM-driven)
  - compute a behavior descriptor + cell for an entity
  - score an entity against a set of opponents/challenges (fitness)

The outer DRQ loop and the MAP-Elites inner loop are entirely domain-agnostic;
everything domain-specific lives behind this Protocol.

Contract shape (see also the engine, which is the single place that resolves
optional hooks):

  * `Domain` below is the REQUIRED core — `isinstance(x, Domain)` means "x has
    the methods the engine always calls". These are structural (`...`) stubs; a
    Protocol cannot supply inherited default bodies, so we do not pretend to.
  * `Opponent` is the structural contract for the object `wrap_opponent` returns
    and the engine appends to the Red Queen history each round.
  * OPTIONAL HOOKS are NOT part of the checked core. The engine resolves each via
    `getattr(domain, name, <default>)`, so a domain simply omits what it doesn't
    need — there is no inheritance to rely on. Recognized hooks + engine defaults:

        is_coevolutionary()            -> False
        new_challenge(llm, target)     -> None
        wrap_opponent(round, chs)      -> engine._DefaultOpponent(round, chs)
        summarize_opponent(opponent)   -> [getattr(c, "tags", []) for c in opp.challenges]
        genome_to_json(genome)         -> genome   (identity; for champions.json / run.jsonl)
        genome_from_json(raw)          -> raw       (identity; inverse, for generality)
            ^ define these two together or neither: if a domain serializes a
              structured genome via genome_to_json it MUST provide the inverse,
              else generality feeds the raw JSON straight to score_challenges.
        pop_timing()                   -> {}
        mock_reply(system, user, role) -> None
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from ..llm import LLMClient


@runtime_checkable
class Opponent(Protocol):
    """The opponent type a domain's `wrap_opponent` produces and the engine
    appends to the history. The engine reads `.challenges` and `.to_dict()`
    (and `.round` for serialization)."""
    round: int
    challenges: Sequence[Any]

    def to_dict(self) -> dict: ...


@runtime_checkable
class Domain(Protocol):
    """Required core. `isinstance(x, Domain)` == 'x is a usable domain core'."""
    name: str
    seed_challenges: Sequence  # fallback / fixed challenge list

    def system_prompt(self) -> str: ...

    def new_genome(self, llm: LLMClient) -> Any:
        """Ask the LLM for a fresh entity genome."""
        ...

    def mutate(self, llm: LLMClient, parent: Any) -> Any:
        """Ask the LLM to refine an existing genome."""
        ...

    def behavior(self, genome: Any, eval_ctx: dict) -> tuple[float, ...]:
        """Raw behavioral descriptor for the genome."""
        ...

    def cell(self, behavior: tuple[float, ...]) -> tuple[int, ...]:
        """Discretize a behavior descriptor into an archive cell."""
        ...

    def fitness(self, genome: Any, opponents: Sequence[Any], seed: int,
                worker_llm: "LLMClient | None" = None) -> tuple[float, tuple[float, ...], dict]:
        """Score `genome` in the environment defined by `opponents`.

        Returns (fitness, raw_behavior, meta). Higher fitness is better.
        """
        ...

    def score_challenges(self, genome: Any, challenges: list,
                         worker_llm: "LLMClient | None" = None) -> dict:
        """Evaluate genome against a specific challenge list. The single
        evaluation kernel — both `fitness()` and generality delegate here.
        Returns at minimum {"accuracy": float, "n_challenges": int, "per_tag": dict}.
        """
        ...


@runtime_checkable
class CoevolutionaryDomain(Domain, Protocol):
    """Informational typed target for co-evolutionary domains (the adversary
    hooks). Not required at runtime — the engine resolves these via getattr."""

    def is_coevolutionary(self) -> bool: ...

    def new_challenge(self, llm: LLMClient, target_genome: Any) -> Any: ...

    def wrap_opponent(self, round_idx: int, challenges: list) -> Opponent: ...
