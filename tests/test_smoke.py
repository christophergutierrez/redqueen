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
