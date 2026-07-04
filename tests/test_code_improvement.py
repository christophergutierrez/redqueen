"""Tests for the code_improvement domain.

Scoring is exercised with an *injected stub worker* (not mock mode), because the
mock LLM cannot produce valid Python patches — this lets us assert the objective
fitness signal is real (gold -> 1.0, wrong -> 0.0) rather than a constant.
"""
import json

import pytest

import drq.domains.code_improvement as ci
from drq.domains.base import Domain
from drq.domains.code_improvement import (
    CodeChallenge, CodeChallengeSet, CodeImprovementDomain, SEED_CHALLENGES,
    _isolation_prefix, extract_patch, run_verify,
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


def test_sandbox_scrubs_secrets(monkeypatch):
    """The sandbox child's OWN environment block must carry no parent secrets
    (defense-in-depth against naive os.environ reads). Before M4 the key was in
    the child env; now it is absent. NOTE: this does NOT prove full protection —
    /proc/<ancestor>/environ can still recover secrets until PID-namespace
    isolation is added (documented residual, deferred follow-up)."""
    # run regardless of host isolation: without namespaces run_verify would
    # refuse by default, but the env scrub is what's under test here.
    monkeypatch.setattr(ci, "_ALLOW_UNSANDBOXED", True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-should-not-leak")
    test = (
        "import os\n"
        "def test_no_secrets():\n"
        "    assert os.environ.get('OPENAI_API_KEY', 'ABSENT') == 'ABSENT'\n"
        "    assert os.environ.get('AWS_SECRET_ACCESS_KEY', 'ABSENT') == 'ABSENT'\n"
    )
    files = {"test_target.py": test}
    assert run_verify(files, "test_target.py", test) is True


def test_sandbox_timeout_reaps_workload():
    """A timed-out infinite loop must leave no orphaned CPU-spinning process.
    Regression: `unshare --fork` orphaned pytest as PID 1 of the namespace until
    `--kill-child` was added. Orphans are attributed via their `drq_ci_` cwd."""
    if not _isolation_prefix():
        pytest.skip("namespaces unavailable; timeout kills pytest directly")
    import glob
    import os
    import time

    files = {"m.py": "x=1\n",
             "test_target.py": "def test():\n    while True:\n        pass\n"}
    assert run_verify(files, "m.py", "x=1\n", timeout=3.0) is False
    time.sleep(1.0)
    survivors = []
    for cwd_link in glob.glob("/proc/[0-9]*/cwd"):
        try:
            if "drq_ci_" in os.readlink(cwd_link):
                survivors.append(os.readlink(cwd_link))
        except OSError:
            pass
    assert not survivors, f"orphaned sandbox processes survived timeout: {survivors}"


def test_sandbox_isolates_proc_ancestors(monkeypatch):
    """The real fix: with namespace isolation, generated code cannot recover a
    parent secret via /proc/<ancestor>/environ. Skips where unprivileged
    namespaces are unavailable (isolation degrades to scrubbed-env only)."""
    if not _isolation_prefix():
        pytest.skip("unprivileged namespaces unavailable on this host")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proc-canary")
    test = (
        "import os\n"
        "def test_no_proc_leak():\n"
        "    hits = []\n"
        "    for pid in os.listdir('/proc'):\n"
        "        if not pid.isdigit():\n"
        "            continue\n"
        "        try:\n"
        "            with open(f'/proc/{pid}/environ', 'rb') as f:\n"
        "                if b'sk-proc-canary' in f.read():\n"
        "                    hits.append(pid)\n"
        "        except OSError:\n"
        "            pass\n"
        "    assert not hits, f'secret recovered via /proc pids {hits}'\n"
    )
    files = {"test_target.py": test}
    assert run_verify(files, "test_target.py", test) is True


# --------------------------------------------------------------------------- #
# Tribunal fixes — fitness-signal forgery, HOME scrub, unsandboxed refusal     #
# --------------------------------------------------------------------------- #


def test_exit_zero_cannot_forge_pass():
    """CRITICAL: the pass signal is pytest's JUnit report, not the exit code. A
    patch that `os._exit(0)`s during collection (never fixing the bug) must NOT
    score as a pass, while the genuine fix still does."""
    if not _isolation_prefix():
        pytest.skip("namespaces unavailable; run_verify refuses by default")
    files = {
        "target.py": "def f():\n    return 1\n",              # bug: should return 2
        "test_target.py": "import target\ndef test_f():\n    assert target.f() == 2\n",
    }
    # forge: exit 0 before pytest can write a report -> no report -> miss
    assert run_verify(files, "target.py", "import os\nos._exit(0)\n") is False
    # sys.exit at import is likewise not a pass
    assert run_verify(files, "target.py", "import sys\nsys.exit(0)\n") is False
    # the real fix genuinely passes
    assert run_verify(files, "target.py", "def f():\n    return 2\n") is True


def test_home_is_scrubbed_to_sandbox():
    """HOME must point at the throwaway sandbox, not the real home dir, so
    `~/.ssh`/`~/.aws`-relative reads land in an empty tmpdir."""
    if not _isolation_prefix():
        pytest.skip("namespaces unavailable; run_verify refuses by default")
    test = (
        "import os\n"
        "def test_home_sandboxed():\n"
        "    h = os.environ.get('HOME', '')\n"
        "    assert 'drq_ci_' in h\n"                       # the sandbox, not real home
        "    assert os.path.realpath(h) == os.path.realpath(os.getcwd())\n"
    )
    files = {"test_target.py": test}
    assert run_verify(files, "test_target.py", test) is True


def test_home_not_in_env_allowlist():
    """HOME must never be inherited from the parent (it would leak the real home
    path and point generated code at ~/.ssh, ~/.aws)."""
    assert "HOME" not in ci._ENV_ALLOWLIST


def test_unsandboxed_refused_without_optin(monkeypatch):
    """When namespaces are unavailable, run_verify must refuse (warn once, return
    False) rather than execute untrusted code bare — unless explicitly opted in."""
    monkeypatch.setattr(ci, "_isolation_prefix", lambda: ())   # force no isolation
    monkeypatch.setattr(ci, "_ALLOW_UNSANDBOXED", False)
    monkeypatch.setattr(ci, "_UNSANDBOXED_WARNED", False)
    files = {"test_target.py": "def test_ok():\n    assert True\n"}
    with pytest.warns(RuntimeWarning, match="unsandboxed"):
        assert run_verify(files, "test_target.py",
                          "def test_ok():\n    assert True\n") is False
    # with the explicit opt-in it runs (bare) and can pass
    monkeypatch.setattr(ci, "_ALLOW_UNSANDBOXED", True)
    assert run_verify(files, "test_target.py",
                      "def test_ok():\n    assert True\n") is True


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
# Mock fidelity (M5)                                                           #
# --------------------------------------------------------------------------- #


def test_mock_reply_worker_is_valid_python_but_wrong():
    d = CodeImprovementDomain()
    patch = extract_patch(d.mock_reply("genome-as-system", "fix this", "worker"))
    compile(patch, "<mock>", "exec")                       # syntactically valid
    ch = SEED_CHALLENGES[0]
    assert run_verify(ch.files, ch.target_file, patch) is False  # but does not solve -> 0.0


def test_mock_reply_evolver_is_coding_prompt_not_sql():
    d = CodeImprovementDomain()
    prompt = d.mock_reply("sys", "user", "evolver")
    assert "SQL" not in prompt and "SQL analyst" not in prompt
    assert "python" in prompt.lower()


def test_mock_evolve_code_improvement_champion_is_coding_prompt(tmp_path):
    """End-to-end offline: champion genomes must be coding prompts (not the
    SQL-analyst stand-in), and mock fitness stays 0.0."""
    from drq.config import DRQConfig, LLMConfig, MapElitesConfig
    from drq.engine import DRQ

    cfg = DRQConfig(
        rounds=1, out_dir=str(tmp_path), token_budget=0,
        llm=LLMConfig(mock=True),
        me=MapElitesConfig(iterations=1, init_random=1, batch_size=1,
                           seed_with_champions=False),
    )
    champs = DRQ(CodeImprovementDomain(seed_challenges=[SEED_CHALLENGES[0]]), cfg).run()
    assert champs, "expected at least one champion"
    assert "SQL analyst" not in champs[0].genome
    assert "```python" in champs[0].genome or "Python engineer" in champs[0].genome
    assert champs[0].fitness == 0.0                        # mock-fitness contract held


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
