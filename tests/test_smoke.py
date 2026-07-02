import json
import os
import subprocess
import sys
import tempfile

PYTHON = sys.executable


def run(args, **kwargs):
    return subprocess.run(
        [PYTHON, "run.py"] + args,
        capture_output=True, text=True, **kwargs
    )


def test_mock_evolve_produces_output():
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "DRQ_LLM_MOCK": "1"}
        result = run(
            ["evolve", "--rounds", "2", "--iterations", "4",
             "--init-random", "3", "--batch", "2", "--seed", "7", "--out", tmp],
            env=env,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        assert result.returncode == 0, result.stderr

        champions = os.path.join(tmp, "champions.json")
        assert os.path.exists(champions), "champions.json not created"

        log = os.path.join(tmp, "run.jsonl")
        assert os.path.exists(log), "run.jsonl not created"

        opponents = os.path.join(tmp, "opponents.json")
        assert os.path.exists(opponents), "opponents.json not created"


def test_mock_champions_json_structure():
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "DRQ_LLM_MOCK": "1"}
        run(
            ["evolve", "--rounds", "2", "--iterations", "2",
             "--init-random", "2", "--batch", "2", "--out", tmp],
            env=env,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        with open(os.path.join(tmp, "champions.json")) as f:
            champs = json.load(f)

        assert len(champs) == 2
        for c in champs:
            assert "round" in c
            assert "fitness" in c
            assert "genome" in c
            assert "cell" in c


def test_mock_generality_runs():
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "DRQ_LLM_MOCK": "1"}
        run(
            ["evolve", "--rounds", "2", "--iterations", "2",
             "--init-random", "2", "--batch", "2", "--out", tmp],
            env=env,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        result = run(
            ["generality", "--champions", os.path.join(tmp, "champions.json"), "--out", tmp],
            env=env,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        assert result.returncode == 0, result.stderr

        gen_path = os.path.join(tmp, "generality.json")
        assert os.path.exists(gen_path)

        with open(gen_path) as f:
            curve = json.load(f)
        assert len(curve) == 2
        for entry in curve:
            assert "round" in entry
            assert "generality" in entry
            assert 0.0 <= entry["generality"] <= 1.0


def test_seed_reproducibility():
    """Same seed should produce identical champions.json."""
    env = {**os.environ, "DRQ_LLM_MOCK": "1"}
    cwd = os.path.dirname(os.path.dirname(__file__))
    with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
        args = ["evolve", "--rounds", "2", "--iterations", "2",
                "--init-random", "2", "--batch", "2", "--seed", "99"]
        run(args + ["--out", tmp1], env=env, cwd=cwd)
        run(args + ["--out", tmp2], env=env, cwd=cwd)
        with open(os.path.join(tmp1, "champions.json")) as f:
            c1 = json.load(f)
        with open(os.path.join(tmp2, "champions.json")) as f:
            c2 = json.load(f)
        assert c1 == c2


def test_opponent_history_grows_each_round():
    """Core Red Queen invariant: one new opponent set is added per round."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "DRQ_LLM_MOCK": "1"}
        run(
            ["evolve", "--rounds", "4", "--iterations", "2",
             "--init-random", "2", "--batch", "2", "--out", tmp],
            env=env,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        with open(os.path.join(tmp, "run.jsonl")) as f:
            rounds = [json.loads(line) for line in f]
        assert len(rounds) == 4
        for i, rec in enumerate(rounds):
            assert rec["n_opponents"] == i + 1, (
                f"round {i}: expected {i+1} opponents, got {rec['n_opponents']}"
            )


def test_history_k_limits_active_opponents():
    """With history_k=1 only the most recent opponent set is active for scoring."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "DRQ_LLM_MOCK": "1"}
        run(
            ["evolve", "--rounds", "3", "--iterations", "2",
             "--init-random", "2", "--batch", "2",
             "--history-k", "1", "--out", tmp],
            env=env,
            cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        with open(os.path.join(tmp, "run.jsonl")) as f:
            rounds = [json.loads(line) for line in f]
        # total opponents grows, but active challenge count stays ≤ 1 round's worth
        assert rounds[0]["n_opponents"] == 1
        assert rounds[1]["n_opponents"] == 2
        assert rounds[2]["n_opponents"] == 3
        # with history_k=1 the number of active challenges should not grow unboundedly
        challenges_r1 = rounds[0]["n_challenges_total"]
        challenges_r2 = rounds[1]["n_challenges_total"]
        challenges_r3 = rounds[2]["n_challenges_total"]
        # each round should use approx the same count (only last round active)
        assert challenges_r2 <= challenges_r1 * 2, (
            f"history_k=1 should limit active challenges, got {challenges_r2} vs {challenges_r1}"
        )
        assert challenges_r3 <= challenges_r1 * 2


def test_generality_delegates_to_domain():
    """generality.py must not import text2sql internals directly."""
    import inspect
    import drq.generality as gen_mod
    src = inspect.getsource(gen_mod)
    assert "exec_match" not in src, "generality.py must not call exec_match directly"
    assert "extract_sql" not in src, "generality.py must not call extract_sql directly"
    assert "text2sql" not in src, "generality.py must not import from text2sql"
