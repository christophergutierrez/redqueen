"""MAP-Elites archive: quality-diversity over a discretized behavior space.

Faithful to the paper's inner loop:
  (i)  sample an elite from the archive
  (ii) mutate -> offspring
  (iii)evaluate fitness + behavior descriptor (BD)
  (iv) insert into BD's cell iff it beats the incumbent elite there

BD axes are domain-defined. The archive itself is domain-agnostic: it just
needs each entity to carry a fitness float and an integer cell tuple.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Entity:
    genome: Any                       # the thing being evolved (e.g. a prompt string)
    fitness: float = -math.inf
    cell: tuple[int, ...] = ()
    behavior: tuple[float, ...] = ()  # raw (pre-discretization) descriptor
    meta: dict = field(default_factory=dict)


class MapElites:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.grid: dict[tuple[int, ...], Entity] = {}

    def __len__(self) -> int:
        return len(self.grid)

    def add(self, e: Entity) -> bool:
        """Insert iff cell empty or offspring beats incumbent. Returns True if kept."""
        incumbent = self.grid.get(e.cell)
        if incumbent is None or e.fitness > incumbent.fitness:
            self.grid[e.cell] = e
            return True
        return False

    def sample(self) -> Entity | None:
        if not self.grid:
            return None
        return self.rng.choice(list(self.grid.values()))

    def elites(self) -> list[Entity]:
        return list(self.grid.values())

    def best(self) -> Entity | None:
        if not self.grid:
            return None
        return max(self.grid.values(), key=lambda e: e.fitness)

    def coverage(self) -> int:
        return len(self.grid)

    def qd_score(self) -> float:
        """Sum of elite fitnesses — the standard QD progress metric."""
        return sum(e.fitness for e in self.grid.values())


def log_bin(value: float, lo: float, hi: float, n_bins: int) -> int:
    """Discretize a positive value into a log-spaced bin index (paper uses log space)."""
    value = max(value, lo)
    if hi <= lo:
        return 0
    frac = (math.log(value) - math.log(lo)) / (math.log(hi) - math.log(lo))
    return max(0, min(n_bins - 1, int(frac * n_bins)))


def lin_bin(value: float, lo: float, hi: float, n_bins: int) -> int:
    if hi <= lo:
        return 0
    frac = (value - lo) / (hi - lo)
    return max(0, min(n_bins - 1, int(frac * n_bins)))
