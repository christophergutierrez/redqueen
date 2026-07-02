import math
import random
import pytest
from drq.archive import Entity, MapElites, lin_bin, log_bin


def _entity(fitness, cell=(0,), genome="g"):
    return Entity(genome=genome, fitness=fitness, cell=cell, behavior=(float(fitness),))


def test_add_to_empty_grid():
    me = MapElites(random.Random(0))
    e = _entity(0.5, cell=(1, 2))
    assert me.add(e)
    assert len(me) == 1


def test_add_better_replaces_incumbent():
    me = MapElites(random.Random(0))
    me.add(_entity(0.3, cell=(0,), genome="weak"))
    me.add(_entity(0.8, cell=(0,), genome="strong"))
    assert me.grid[(0,)].genome == "strong"
    assert len(me) == 1


def test_add_worse_keeps_incumbent():
    me = MapElites(random.Random(0))
    me.add(_entity(0.8, cell=(0,), genome="strong"))
    kept = me.add(_entity(0.3, cell=(0,), genome="weak"))
    assert not kept
    assert me.grid[(0,)].genome == "strong"


def test_add_different_cells():
    me = MapElites(random.Random(0))
    me.add(_entity(0.5, cell=(0,)))
    me.add(_entity(0.5, cell=(1,)))
    assert len(me) == 2


def test_best_empty():
    me = MapElites(random.Random(0))
    assert me.best() is None


def test_best_nonempty():
    me = MapElites(random.Random(0))
    me.add(_entity(0.3, cell=(0,)))
    me.add(_entity(0.9, cell=(1,)))
    me.add(_entity(0.1, cell=(2,)))
    assert me.best().fitness == pytest.approx(0.9)


def test_sample_empty():
    me = MapElites(random.Random(0))
    assert me.sample() is None


def test_sample_returns_an_elite():
    me = MapElites(random.Random(0))
    me.add(_entity(0.5, cell=(0,)))
    e = me.sample()
    assert e is not None
    assert e.fitness == pytest.approx(0.5)


def test_coverage_and_qd_score():
    me = MapElites(random.Random(0))
    me.add(_entity(0.2, cell=(0,)))
    me.add(_entity(0.5, cell=(1,)))
    me.add(_entity(0.3, cell=(2,)))
    assert me.coverage() == 3
    assert me.qd_score() == pytest.approx(1.0)


def test_lin_bin_clamp_below():
    assert lin_bin(-5.0, 0.0, 10.0, 5) == 0


def test_lin_bin_clamp_above():
    assert lin_bin(999.0, 0.0, 10.0, 5) == 4


def test_lin_bin_midpoint():
    assert lin_bin(5.0, 0.0, 10.0, 10) == 5


def test_lin_bin_degenerate():
    assert lin_bin(5.0, 3.0, 3.0, 5) == 0


def test_log_bin_clamp_below():
    assert log_bin(0.0, 1.0, 100.0, 5) == 0


def test_log_bin_midpoint_above():
    result = log_bin(10.0, 1.0, 100.0, 4)
    assert 0 <= result <= 3


# ── cell assignment contract ────────────────────────────────────────────────

from drq.domains.text2sql import Text2SQLDomain


def test_cell_stable_for_identical_genomes():
    """Same genome must always map to the same cell (MAP-Elites correctness)."""
    domain = Text2SQLDomain()
    genome = "SELECT only, use CTEs, handle NULLs. Return ONLY the SQL."
    c1 = domain.cell(domain.behavior(genome, {}))
    c2 = domain.cell(domain.behavior(genome, {}))
    assert c1 == c2


def test_cell_different_lengths_produce_different_cells():
    """Prompts of very different lengths should land in distinct length bins."""
    domain = Text2SQLDomain()
    short = "Return SQL only."                  # ~3 words
    long = " ".join(["word"] * 100)             # 100 words — near the max bin
    c_short = domain.cell(domain.behavior(short, {}))
    c_long = domain.cell(domain.behavior(long, {}))
    assert c_short[0] != c_long[0], "short and long prompts must differ in length bin"


def test_cell_reasoning_axis_increases_with_keywords():
    """Prompts with more reasoning keywords should land in a higher reasoning bin."""
    domain = Text2SQLDomain()
    # Use enough reasoning words to cross a bin boundary
    plain = "Return SQL only. No explanation."
    reasoned = "Think step by step. Plan the query. Restate the question first. Use CTEs."
    beh_plain = domain.behavior(plain, {})
    beh_reasoned = domain.behavior(reasoned, {})
    assert beh_reasoned[1] > beh_plain[1], "reasoning score should be higher for reasoned prompt"
    c_plain = domain.cell(beh_plain)
    c_reasoned = domain.cell(beh_reasoned)
    assert c_plain[1] <= c_reasoned[1], "reasoning bin should be >= for reasoned prompt"


def test_cell_dimensions_in_valid_range():
    """Cell indices must fall within [0, n_bins-1] for any genome."""
    domain = Text2SQLDomain()
    genomes = [
        "",
        "x",
        " ".join(["word"] * 200),  # exceeds _PROMPT_LEN_MAX
        "step " * 20,              # high reasoning word count
    ]
    for g in genomes:
        cell = domain.cell(domain.behavior(g, {}))
        assert cell[0] in range(domain.len_bins), f"length bin out of range for {g!r}"
        assert cell[1] in range(domain.reason_bins), f"reason bin out of range for {g!r}"


def test_best_returns_max_unique():
    """best() must return the unique maximum even when multiple entities have high fitness."""
    me = MapElites(random.Random(0))
    me.add(_entity(0.9, cell=(0,), genome="winner"))
    me.add(_entity(0.9, cell=(1,), genome="also-good"))
    me.add(_entity(0.5, cell=(2,), genome="weak"))
    best = me.best()
    assert best.fitness == pytest.approx(0.9)


def test_add_equal_fitness_keeps_incumbent():
    """Equal fitness should NOT displace the incumbent (strictly better required)."""
    me = MapElites(random.Random(0))
    me.add(_entity(0.5, cell=(0,), genome="original"))
    kept = me.add(_entity(0.5, cell=(0,), genome="challenger"))
    assert not kept
    assert me.grid[(0,)].genome == "original"
