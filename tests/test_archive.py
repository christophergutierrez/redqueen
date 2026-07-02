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
