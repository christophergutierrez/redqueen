"""Tests for the code_improvement domain.

Scoring is exercised with an *injected stub worker* (not mock mode), because the
mock LLM cannot produce valid Python patches — this lets us assert the objective
fitness signal is real (gold -> 1.0, wrong -> 0.0) rather than a constant.
"""
import json

from drq.domains.base import Domain
from drq.domains.code_improvement import (
    CodeChallenge, CodeChallengeSet, CodeImprovementDomain, SEED_CHALLENGES,
    extract_patch, run_verify,
)
from drq.llm import ChatResult


# --------------------------------------------------------------------------- #
# Stub workers                                                                 #
# --------------------------------------------------------------------------- #


class GoldWorker:
    """Returns each challenge's gold fix, fenced. Should score 1.0."""
    def __init__(self, content):
        self._content = content

    def chat(self, system, user, max_tokens=None):
        return ChatResult(text=f"```python\n{self._content}\n```")


class WrongWorker:
    """Returns garbage. Should score 0.0."""
    def chat(self, system, user, max_tokens=None):
        return ChatResult(text="```python\nraise RuntimeError('nope')\n```")


class FailWorker:
    """Simulates a failed LLM call (res.ok False) -> must be counted as a miss."""
    def chat(self, system, user, max_tokens=None):
        return ChatResult(text="", ok=False, error="boom")


# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #


def test_extract_patch_fenced():
    assert extract_patch("pre ```python\nx = 1\n``` post") == "x = 1"


def test_extract_patch_plain_fence():
    assert extract_patch("```\ny = 2\n```") == "y = 2"


def test_extract_patch_no_fence():
    assert extract_patch("z = 3") == "z = 3"


# --------------------------------------------------------------------------- #
# Sandbox kernel (Phase 1 gates)                                               #
# --------------------------------------------------------------------------- #


def test_sandbox_gold_passes():
    ch = SEED_CHALLENGES[0]
    assert run_verify(ch.files, ch.target_file, ch.gold_content) is True


def test_sandbox_buggy_fails():
    ch = SEED_CHALLENGES[0]
    buggy = ch.files[ch.target_file]
    assert run_verify(ch.files, ch.target_file, buggy) is False


def test_sandbox_rejects_escape(tmp_path):
    """A traversal target_file must be refused and write nothing outside the sandbox."""
    sentinel = tmp_path / "evil.py"
    files = {"test_target.py": "def test_x():\n    assert True\n"}
    # target_file climbs out of the sandbox toward a real path
    ok = run_verify(files, "../../../../../../../../evil.py", "x = 1")
    assert ok is False
    assert not sentinel.exists()


def test_sandbox_rejects_empty_target_file():
    files = {"test_target.py": "def test_x():\n    assert True\n"}
    assert run_verify(files, "", "x = 1") is False


def test_sandbox_rejects_root_file_entry():
    files = {"": "x = 1", "test_target.py": "def test_x():\n    assert True\n"}
    assert run_verify(files, "target.py", "x = 1") is False


def test_sandbox_rejects_directory_target():
    files = {"pkg/mod.py": "x = 1", "test_target.py": "def test_x():\n    assert True\n"}
    assert run_verify(files, "pkg", "x = 2") is False


def test_sandbox_timeout():
    """A target that loops forever on import must return False within the timeout."""
    files = {
        "loop.py": "while True:\n    pass\n",
        "test_target.py": "import loop\ndef test_x():\n    assert True\n",
    }
    assert run_verify(files, "loop.py", "while True:\n    pass\n", timeout=3.0) is False


# --------------------------------------------------------------------------- #
# Domain: Protocol conformance + behavior                                      #
# --------------------------------------------------------------------------- #


def test_protocol_conformance():
    assert isinstance(CodeImprovementDomain(), Domain)


def test_is_coevolutionary():
    assert CodeImprovementDomain().is_coevolutionary() is True


def test_behavior_and_cell_shapes():
    d = CodeImprovementDomain()
    beh = d.behavior("test first, then make the minimal edge-case fix", {})
    assert len(beh) == 2
    cell = d.cell(beh)
    assert len(cell) == 2
    assert all(isinstance(i, int) for i in cell)


# --------------------------------------------------------------------------- #
# score_challenges: non-vacuous + correct key set                             #
# --------------------------------------------------------------------------- #


def test_score_keys():
    d = CodeImprovementDomain()
    ch = SEED_CHALLENGES[0]
    res = d.score_challenges("genome", [ch], GoldWorker(ch.gold_content))
    assert {"accuracy", "n_challenges", "per_tag"} <= set(res)


def test_score_gold_is_one():
    d = CodeImprovementDomain()
    ch = SEED_CHALLENGES[1]
    res = d.score_challenges("genome", [ch], GoldWorker(ch.gold_content))
    assert res["accuracy"] == 1.0
    assert res["n_challenges"] == 1


def test_score_wrong_is_zero():
    d = CodeImprovementDomain()
    ch = SEED_CHALLENGES[1]
    res = d.score_challenges("genome", [ch], WrongWorker())
    assert res["accuracy"] == 0.0


def test_score_failed_call_is_miss():
    d = CodeImprovementDomain()
    ch = SEED_CHALLENGES[0]
    res = d.score_challenges("genome", [ch], FailWorker())
    assert res["accuracy"] == 0.0


def test_fitness_delegates_and_falls_back_to_seed():
    """No opponents -> fitness scores against seed_challenges (one, for speed)."""
    d = CodeImprovementDomain(seed_challenges=[SEED_CHALLENGES[2]])
    ch = d.seed_challenges[0]
    acc, beh, meta = d.fitness("genome", [], seed=0, worker_llm=GoldWorker(ch.gold_content))
    assert acc == 1.0
    assert meta["n_challenges"] == 1
    assert len(beh) == 2


# --------------------------------------------------------------------------- #
# Admission: buggy must fail AND gold must pass                                #
# --------------------------------------------------------------------------- #


class ChallengeWorker:
    """An 'adversary' evolver returning a fixed JSON challenge."""
    def __init__(self, payload):
        self._payload = payload

    def chat(self, system, user, max_tokens=None):
        return ChatResult(text=json.dumps(self._payload))


_GOOD_PAYLOAD = {
    "task": "inc must add one",
    "target_file": "m.py",
    "buggy_content": "def inc(x):\n    return x\n",
    "gold_content": "def inc(x):\n    return x + 1\n",
    "test_content": "from m import inc\ndef test_inc():\n    assert inc(1) == 2\n",
    "tags": ["arith"],
}


def test_admission_accepts_valid_challenge():
    d = CodeImprovementDomain()
    ch = d.new_challenge(ChallengeWorker(_GOOD_PAYLOAD), "any genome")
    assert ch is not None
    assert ch.target_file == "m.py"
    assert "test_target.py" in ch.files


def test_admission_rejects_when_buggy_passes():
    """If the 'buggy' content already satisfies the test, the challenge is vacuous."""
    payload = dict(_GOOD_PAYLOAD, buggy_content="def inc(x):\n    return x + 1\n")
    d = CodeImprovementDomain()
    assert d.new_challenge(ChallengeWorker(payload), "g") is None


def test_admission_rejects_when_gold_fails():
    """If the gold fix does not pass the test, the challenge is unsolvable."""
    payload = dict(_GOOD_PAYLOAD, gold_content="def inc(x):\n    return x\n")
    d = CodeImprovementDomain()
    assert d.new_challenge(ChallengeWorker(payload), "g") is None


def test_admission_rejects_malformed_json():
    class Junk:
        def chat(self, system, user, max_tokens=None):
            return ChatResult(text="not json at all")
    d = CodeImprovementDomain()
    assert d.new_challenge(Junk(), "g") is None


def test_admission_ignores_injected_command():
    """A model-supplied 'command' key must be ignored (verify command is fixed)."""
    payload = dict(_GOOD_PAYLOAD, command=["rm", "-rf", "/"], verify_cmd="evil")
    d = CodeImprovementDomain()
    ch = d.new_challenge(ChallengeWorker(payload), "g")
    assert ch is not None
    assert not hasattr(ch, "command")
    assert "command" not in ch.to_dict()


# --------------------------------------------------------------------------- #
# Opponent shape (engine coupling contract)                                    #
# --------------------------------------------------------------------------- #


def test_opponent_shape():
    d = CodeImprovementDomain()
    cs = d.wrap_opponent(3, [SEED_CHALLENGES[0]])
    assert isinstance(cs, CodeChallengeSet)
    assert cs.round == 3
    assert len(cs.challenges) == 1
    # engine.py reaches into these three (engine.py:117,124,144)
    assert isinstance(cs.challenges[0].tags, list)
    assert "round" in cs.to_dict()
    assert "challenges" in cs.to_dict()


def test_challenge_roundtrips_through_dict():
    ch = SEED_CHALLENGES[0]
    d = ch.to_dict()
    rebuilt = CodeChallenge(**d)
    assert rebuilt.target_file == ch.target_file
    assert rebuilt.gold_content == ch.gold_content


# --------------------------------------------------------------------------- #
# Timing instrumentation                                                       #
# --------------------------------------------------------------------------- #


def test_eval_timer_accumulates_and_resets():
    from drq.timing import EvalTimer
    t = EvalTimer()
    t.add_llm(0.5)
    t.add_verify(0.2)
    t.add_verify(0.3)
    d = t.pop()
    assert d["llm_s"] == 0.5
    assert d["verify_s"] == 0.5
    assert d["llm_calls"] == 1
    assert d["verify_calls"] == 2
    # pop resets to zero
    d2 = t.pop()
    assert d2 == {"llm_s": 0.0, "verify_s": 0.0, "llm_calls": 0, "verify_calls": 0}


def test_score_populates_timing_and_resets():
    d = CodeImprovementDomain()
    ch = SEED_CHALLENGES[0]
    d.score_challenges("genome", [ch], GoldWorker(ch.gold_content))
    timing = d.pop_timing()
    assert timing["llm_calls"] == 1       # one worker chat
    assert timing["verify_calls"] == 1    # one sandbox pytest run
    assert timing["verify_s"] > 0.0       # subprocess actually ran
    # popped -> next round starts clean
    assert d.pop_timing()["llm_calls"] == 0


def test_failed_call_records_llm_but_not_verify():
    """A failed worker call is timed as an LLM call but skips verification."""
    d = CodeImprovementDomain()
    ch = SEED_CHALLENGES[0]
    d.score_challenges("genome", [ch], FailWorker())
    timing = d.pop_timing()
    assert timing["llm_calls"] == 1
    assert timing["verify_calls"] == 0
