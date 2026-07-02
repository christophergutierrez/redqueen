"""Domain interface.

A DRQ domain must know how to:
  - describe itself to the LLM (system prompt)
  - generate a fresh entity genome and mutate an existing one (LLM-driven)
  - compute a behavior descriptor + cell for an entity
  - score an entity against a set of opponents/challenges (fitness)

The outer DRQ loop and the MAP-Elites inner loop are entirely domain-agnostic;
everything domain-specific lives behind this Protocol.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable

from ..archive import Entity
from ..llm import LLMClient


@runtime_checkable
class Domain(Protocol):
    name: str

    def system_prompt(self) -> str: ...

    def new_genome(self, llm: LLMClient) -> Any:
        """Ask the LLM for a fresh entity genome."""
        ...

    def mutate(self, llm: LLMClient, parent: Any) -> Any:
        """Ask the LLM to refine an existing genome."""
        ...

    def behavior(self, genome: Any, eval_ctx: dict) -> tuple[float, ...]:
        """Raw behavioral descriptor, filled during evaluation."""
        ...

    def cell(self, behavior: tuple[float, ...]) -> tuple[int, ...]:
        """Discretize a behavior descriptor into an archive cell."""
        ...

    def fitness(self, genome: Any, opponents: Sequence[Any], seed: int,
               worker_llm: "LLMClient | None" = None) -> tuple[float, tuple[float, ...], dict]:
        """Score `genome` in the environment defined by `opponents`.

        Returns (fitness, raw_behavior, meta). This is where the simulation /
        LLM-judge / metric lives. Higher fitness is better.
        """
        ...

    # --- adversarial / co-evolution hooks (optional; default no-op) -----------
    def new_challenge(self, llm: LLMClient, target_genome: Any) -> Any:
        """Adversary population: propose a challenge that breaks `target_genome`."""
        return None

    def wrap_opponent(self, round_idx: int, challenges: list) -> Any:
        """Package a list of challenges into the domain's opponent type."""
        return challenges

    def is_coevolutionary(self) -> bool:
        return False
