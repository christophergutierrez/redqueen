"""Code-improvement domain: genuine Red Queen co-evolution over *engineering prompts*.

Two populations, exactly parallel to text2sql:
  SOLVER    — an LLM *system prompt* for a coding agent (the evolved entity). Given
              a task and the current contents of a single Python file, the worker
              LLM uses this prompt to emit the corrected FULL file. Good prompts
              generalize across bug shapes.
  CHALLENGE — a self-contained mini-project: a set of files (a buggy target module
              + a pytest test + a proven gold fix). The adversary evolves challenges
              the current champion solver gets wrong.

Fitness of a solver = fraction of challenges it fixes, where "fixes" is OBJECTIVE:
the worker's patch is written into an ephemeral sandbox and a FIXED pytest command
is run; success == pytest's own JUnit report shows ≥1 test ran and none failed. The
signal is the REPORT, not the raw exit code — a patch that does `os._exit(0)` during
collection never lets pytest write the report, so it can't forge a pass. This mirrors
text2sql's execution-accuracy (`exec_match` on a throwaway DuckDB) — here the throwaway
resource is a tmpdir instead of an in-memory database. No LLM judging.

Safety (this domain executes model-generated Python):
  - each evaluation runs in its own `tempfile.TemporaryDirectory` (transient, isolated);
  - the verify command is the module constant `VERIFY_CMD` — NEVER sourced from model
    output, so a challenge cannot inject an arbitrary shell command;
  - every written path is confined to the sandbox root (traversal is refused);
  - the pass/fail signal is a JUnit report written to a parent-controlled path OUTSIDE
    the sandbox and validated by the parent (`_report_passed`), not the child's exit code;
  - when unprivileged namespaces are available, the subprocess runs inside a fresh
    PID + network + mount namespace (via `unshare`, see `_isolation_prefix`): `/proc`
    shows only sandbox processes — so ancestor environments (e.g. the engine's or the
    launching shell's `OPENAI_API_KEY`) are NOT recoverable via `/proc/<pid>/environ` —
    and network egress is cut;
  - the interpreter runs under `-I` with a SCRUBBED environment (`_sandbox_env`); HOME is
    reset to the sandbox root, so `~`-relative reads (`~/.ssh`, `~/.aws`) miss the real home;
  - the child runs in its own process group and a timeout SIGKILLs the WHOLE group
    (`_run_child`), so a forked background spinner cannot outlive the verify;
  - if unprivileged namespaces are unavailable, run_verify REFUSES by default (warns once,
    scores every challenge as a miss) rather than run untrusted code bare — set
    DRQ_ALLOW_UNSANDBOXED=1 to override, only with a trusted model.

Residual (documented): the isolated sandbox still shares the host FILESYSTEM, so generated
code can read files the user can (e.g. `~/.aws/credentials` via an absolute path); and a
maximally-adversarial patch could read its own argv to find the JUnit path and forge a
passing report. Both are closed by a read-only-bind mount (bwrap `--ro-bind` / `pivot_root`
to a minimal rootfs), the planned next hardening step.

Behavior descriptor (for MAP-Elites diversity):
  axis 0: prompt length in words
  axis 1: "process-ness" — does the prompt push a disciplined strategy
           (test-first, minimal diff, edge cases, verification)?
"""
from __future__ import annotations

import functools
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Sequence

from ..archive import lin_bin
from ..llm import LLMClient
from ..timing import EvalTimer

# The verify command is FIXED here and never read from a challenge / model output.
# -I isolates the interpreter (ignores PYTHON* env / user site / cwd on sys.path);
# pytest still inserts the sandbox dir so the target module imports.
VERIFY_CMD = [sys.executable, "-I", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
_VERIFY_TIMEOUT_S = 30.0

# Env vars the verify subprocess is allowed to see. Deny-by-default: everything
# else (OPENAI_API_KEY, cloud creds, ...) is withheld from model-generated code.
# HOME is deliberately NOT inherited — it would point generated code straight at
# ~/.ssh, ~/.aws, ~/.config; run_verify sets HOME to the sandbox root instead.
_ENV_ALLOWLIST = ("PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "SystemRoot")

# Set DRQ_ALLOW_UNSANDBOXED=1 to permit running model code when unprivileged
# namespaces are unavailable (see run_verify). Off by default: fail safe.
_ALLOW_UNSANDBOXED = os.environ.get("DRQ_ALLOW_UNSANDBOXED") == "1"


def _sandbox_env(home: str | None = None) -> dict:
    """A scrubbed environment for the verify subprocess so generated code cannot
    read secrets from the parent process. Only the few vars needed to run
    python + pytest are kept. `home` (the sandbox root) becomes HOME so any
    `~`-relative access stays inside the throwaway dir, not the real home."""
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.setdefault("PATH", os.defpath)
    env["HOME"] = home or os.path.join(os.sep, "nonexistent")
    env["PYTHONHASHSEED"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


# unshare into a fresh user+PID+net+mount namespace: /proc then shows only the
# sandbox's own processes (hiding ancestor environments) and network is cut.
# --map-root-user makes it work without privileges where unprivileged userns is on.
# --kill-child (via PR_SET_PDEATHSIG) makes the forked pytest die when unshare is
# SIGKILLed on timeout — else it orphans as PID 1 of the ns and spins forever.
_UNSHARE_ARGS = ("--user", "--map-root-user", "--pid", "--fork", "--kill-child",
                 "--mount-proc", "--net", "--")


@functools.lru_cache(maxsize=1)
def _isolation_prefix() -> tuple[str, ...]:
    """argv prefix that runs a command in an isolating namespace, or () if the
    host can't do it unprivileged (then run_verify degrades to scrubbed-env only).
    Probed once and cached."""
    exe = shutil.which("unshare")
    if not exe:
        return ()
    try:
        probe = subprocess.run(
            [exe, *_UNSHARE_ARGS, "true"],
            capture_output=True, timeout=10, env=_sandbox_env(),
        )
        if probe.returncode == 0:
            return (exe, *_UNSHARE_ARGS)
    except Exception:  # noqa: BLE001 - any failure -> no isolation available
        pass
    return ()

# --------------------------------------------------------------------------- #
# Challenge representation                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class CodeChallenge:
    """A self-contained mini-project the solver must repair.

    `files` maps sandbox-relative path -> content and includes the buggy target
    plus a `test_*.py`. `target_file` is the single file the solver may rewrite;
    its current (buggy) contents are `files[target_file]`. `gold_content` is a
    proven fix used only for the admission check (the analog of text2sql gold_sql).
    """
    task: str
    files: dict[str, str]
    target_file: str
    gold_content: str
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"task": self.task, "files": self.files,
                "target_file": self.target_file, "gold_content": self.gold_content,
                "tags": self.tags}


@dataclass
class CodeChallengeSet:
    """A champion opponent: the hard challenges from one adversary round."""
    round: int
    challenges: list[CodeChallenge]

    def to_dict(self) -> dict:
        return {"round": self.round, "challenges": [c.to_dict() for c in self.challenges]}


# --------------------------------------------------------------------------- #
# Sandboxed execution kernel (the throwaway-tmpdir analog of text2sql._run)    #
# --------------------------------------------------------------------------- #


def _resolve_in(root: str, rel: str) -> str | None:
    """Resolve `rel` under `root`; return the absolute path iff it stays inside
    the sandbox, else None (traversal / absolute-path escape refused)."""
    if not rel:
        return None
    dest = os.path.realpath(os.path.join(root, rel))
    root_real = os.path.realpath(root)
    if dest != root_real and dest.startswith(root_real + os.sep):
        return dest
    return None


_UNSANDBOXED_WARNED = False


def _warn_unsandboxed_once() -> None:
    global _UNSANDBOXED_WARNED
    if not _UNSANDBOXED_WARNED:
        _UNSANDBOXED_WARNED = True
        warnings.warn(
            "code_improvement: unprivileged namespaces unavailable — refusing to "
            "run model-generated code unsandboxed (every verify scores as a miss). "
            "On such a host, generated code could read parent secrets via "
            "/proc/<ancestor>/environ and reach the network. Set "
            "DRQ_ALLOW_UNSANDBOXED=1 to override (only with a trusted model).",
            RuntimeWarning, stacklevel=3,
        )


def _report_passed(report_path: str) -> bool:
    """Validate a pytest JUnit-XML report the parent controls: at least one test
    actually ran and NONE failed/errored. This is what makes the fitness signal
    resistant to exit-code forgery — a patch that does `os._exit(0)` during
    collection never lets pytest write the report, so parsing fails -> miss."""
    try:
        root = ET.parse(report_path).getroot()
    except (ET.ParseError, FileNotFoundError, OSError):
        return False
    total = fails = errs = skips = 0
    for suite in root.iter("testsuite"):
        total += int(suite.get("tests", 0))
        fails += int(suite.get("failures", 0))
        errs += int(suite.get("errors", 0))
        skips += int(suite.get("skipped", 0))
    return fails == 0 and errs == 0 and (total - skips) > 0


def _run_child(cmd: list[str], cwd: str, timeout: float, env: dict) -> None:
    """Run `cmd` in its OWN session/process-group and, on timeout, SIGKILL the
    whole group so a patch that forks a background CPU-spinner cannot outlive the
    verify (belt-and-suspenders with unshare --kill-child on the isolated path)."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.communicate()


def run_verify(files: dict[str, str], target_file: str, replacement: str,
               timeout: float = _VERIFY_TIMEOUT_S) -> bool:
    """Materialize `files` in a fresh tmpdir, overwrite `target_file` with
    `replacement`, run the FIXED pytest command, and return True iff pytest's
    own JUnit report shows ≥1 test ran and none failed.

    The pass signal is the report (written to a parent-controlled path OUTSIDE
    the sandbox), NOT the process exit code — so an in-process `os._exit(0)` /
    `sys.exit(0)` during collection can't forge a pass. Any path escaping the
    sandbox, a timeout, or a missing/failed report returns False. When isolation
    is unavailable and DRQ_ALLOW_UNSANDBOXED is unset, run_verify refuses (warns
    once, returns False) rather than execute untrusted code bare. The directory
    is always torn down.
    """
    prefix = _isolation_prefix()
    if not prefix and not _ALLOW_UNSANDBOXED:
        _warn_unsandboxed_once()
        return False
    with tempfile.TemporaryDirectory(prefix="drq_ci_") as root:
        to_write = dict(files)
        to_write[target_file] = replacement
        for rel, content in to_write.items():
            dest = _resolve_in(root, rel)
            if dest is None:
                return False  # path escape -> treat as failure, write nothing outside
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.isdir(dest):
                    return False
                with open(dest, "w") as f:
                    f.write(content)
            except OSError:
                return False
        # JUnit report lives in its OWN tmpdir, not `root`, so the child would
        # have to both discover (via its argv) and forge it to fake a pass.
        report_dir = tempfile.mkdtemp(prefix="drq_ci_rpt_")
        report = os.path.join(report_dir, "r.xml")
        cmd = [*prefix, *VERIFY_CMD, f"--junitxml={report}"]
        try:
            _run_child(cmd, cwd=root, timeout=timeout, env=_sandbox_env(home=root))
            return _report_passed(report)
        finally:
            shutil.rmtree(report_dir, ignore_errors=True)


_CODE_FENCE = re.compile(r"```(?:python|py)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_patch(text: str) -> str:
    """Pull the full-file replacement out of a fenced code block; fall back to
    the raw text. Mirrors text2sql.extract_sql."""
    m = _CODE_FENCE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


# --------------------------------------------------------------------------- #
# Seed challenges — self-contained mini-projects encoding repo-relevant bugs   #
# (patterns, NOT the live repo: hermetic, no self-modification — see plan N2)  #
# --------------------------------------------------------------------------- #


def _challenge(task, target, buggy, gold, test, tags) -> CodeChallenge:
    return CodeChallenge(
        task=task,
        files={target: buggy, "test_target.py": test},
        target_file=target,
        gold_content=gold,
        tags=tags,
    )


SEED_CHALLENGES: list[CodeChallenge] = [
    # (a) MAP-Elites cell replacement must be STRICTLY better (no replace on tie).
    _challenge(
        task=("Cell.add should keep the incumbent on a fitness TIE and only replace "
              "when the new fitness is strictly greater. Fix the comparison."),
        target="cell.py",
        buggy=("class Cell:\n"
               "    def __init__(self):\n"
               "        self.best = None\n"
               "    def add(self, fitness):\n"
               "        if self.best is None or fitness >= self.best:\n"
               "            self.best = fitness\n"
               "            return True\n"
               "        return False\n"),
        gold=("class Cell:\n"
              "    def __init__(self):\n"
              "        self.best = None\n"
              "    def add(self, fitness):\n"
              "        if self.best is None or fitness > self.best:\n"
              "            self.best = fitness\n"
              "            return True\n"
              "        return False\n"),
        test=("from cell import Cell\n"
              "def test_tie_does_not_replace():\n"
              "    c = Cell()\n"
              "    assert c.add(1.0) is True\n"
              "    assert c.add(1.0) is False\n"
              "def test_strictly_greater_replaces():\n"
              "    c = Cell()\n"
              "    c.add(1.0)\n"
              "    assert c.add(2.0) is True\n"),
        tags=["archive", "comparison"],
    ),
    # (b) Linear binning must clamp to [0, n_bins-1] and handle hi <= lo.
    _challenge(
        task=("lin_bin must clamp the returned index into [0, n_bins-1] for values "
              "at or beyond the range boundaries. Fix the out-of-range results."),
        target="binning.py",
        buggy=("def lin_bin(value, lo, hi, n_bins):\n"
               "    frac = (value - lo) / (hi - lo)\n"
               "    return int(frac * n_bins)\n"),
        gold=("def lin_bin(value, lo, hi, n_bins):\n"
              "    if hi <= lo:\n"
              "        return 0\n"
              "    frac = (value - lo) / (hi - lo)\n"
              "    return max(0, min(n_bins - 1, int(frac * n_bins)))\n"),
        test=("from binning import lin_bin\n"
              "def test_upper_bound_clamped():\n"
              "    assert lin_bin(10, 0, 10, 5) == 4\n"
              "def test_lower_bound_clamped():\n"
              "    assert lin_bin(-5, 0, 10, 5) == 0\n"
              "def test_middle():\n"
              "    assert lin_bin(5, 0, 10, 5) == 2\n"),
        tags=["binning", "bounds"],
    ),
    # (c) CLI argument default is missing.
    _challenge(
        task=("build_parser must give --rounds a default of 12 so parsing an empty "
              "argument list yields rounds == 12."),
        target="cli.py",
        buggy=("import argparse\n"
               "def build_parser():\n"
               "    p = argparse.ArgumentParser()\n"
               "    p.add_argument('--rounds', type=int)\n"
               "    return p\n"),
        gold=("import argparse\n"
              "def build_parser():\n"
              "    p = argparse.ArgumentParser()\n"
              "    p.add_argument('--rounds', type=int, default=12)\n"
              "    return p\n"),
        test=("from cli import build_parser\n"
              "def test_default_rounds():\n"
              "    args = build_parser().parse_args([])\n"
              "    assert args.rounds == 12\n"),
        tags=["cli", "argparse"],
    ),
    # (d) Fenced-block extractor ignores its match and returns the whole text.
    _challenge(
        task=("extract_sql must return the contents INSIDE the first triple-backtick "
              "code fence, stripped. Fix it to use the regex match."),
        target="extract.py",
        buggy=("import re\n"
               "def extract_sql(text):\n"
               "    m = re.search(r'```(.*?)```', text, re.DOTALL)\n"
               "    return text\n"),
        gold=("import re\n"
              "def extract_sql(text):\n"
              "    m = re.search(r'```(.*?)```', text, re.DOTALL)\n"
              "    return m.group(1).strip() if m else text.strip()\n"),
        test=("from extract import extract_sql\n"
              "def test_fence_extracted():\n"
              "    assert extract_sql('note ```SELECT 1``` end') == 'SELECT 1'\n"),
        tags=["parsing", "regex"],
    ),
]


# --------------------------------------------------------------------------- #
# Domain                                                                       #
# --------------------------------------------------------------------------- #

_PROMPT_LEN_MAX = 120     # words; upper bound for BD normalization
_PROCESS_WORDS = ("test", "minimal", "edge", "verify", "first", "diff", "check",
                  "reproduce", "smallest", "regression")

# Snippets the offline mock evolver samples to build varied coding prompts.
_MOCK_CODING_TRICKS = (
    "Make the smallest change that turns the failing test green.",
    "Restate the bug, then edit only the responsible line.",
    "Check edge cases (empty, None, zero) before finalizing.",
    "Preserve the public API; touch just the target file.",
    "Reproduce the failure mentally, then write the minimal fix.",
    "Prefer a guard clause over restructuring.",
)


class CodeImprovementDomain:
    name = "code_improvement"

    def __init__(self, seed_challenges: list[CodeChallenge] | None = None,
                 len_bins: int = 5, process_bins: int = 3):
        self.seed_challenges = seed_challenges or list(SEED_CHALLENGES)
        self.len_bins = len_bins
        self.process_bins = process_bins
        self.timer = EvalTimer()
        # only used by mock_reply's (sequential, main-thread) evolver branch
        self._mock_rng = random.Random(0)

    def pop_timing(self) -> dict:
        """Return and reset accumulated LLM vs verify timing for the last round."""
        return self.timer.pop()

    def mock_reply(self, system: str, user: str, role: str) -> "str | None":
        """Domain-faithful offline responses so DRQ_LLM_MOCK=1 runs evolve *coding*
        prompts (not the generic SQL-analyst stand-in). The worker returns a valid
        but non-solving patch, so mock fitness stays 0.0 while the real
        extract_patch -> run_verify path is exercised."""
        if role == "evolver":
            # new_genome / mutate -> a varied coding-agent system prompt.
            # (new_challenge also lands here; non-JSON -> rejected -> seed fallback,
            #  same as before, so offline coevolution behavior is unchanged.)
            k = self._mock_rng.randint(2, 3)
            body = " ".join(self._mock_rng.sample(_MOCK_CODING_TRICKS, k))
            return (f"You are a meticulous Python engineer. {body} "
                    "Return the corrected FULL file in one ```python block.")
        # worker -> a syntactically valid file that does NOT fix the bug
        return "```python\n# offline mock patch: not a real fix\nraise NotImplementedError\n```"

    # -- LLM description -----------------------------------------------------
    def system_prompt(self) -> str:
        return (
            "You are evolving SYSTEM PROMPTS for a downstream Python coding agent. "
            "A good system prompt makes a language model reliably repair a single "
            "Python file so a project's tests pass. Given a task and the file's "
            "current contents, the agent must return the corrected FULL file. Prompts "
            "should push disciplined, test-passing, minimal fixes across diverse bugs. "
            "Output ONLY the prompt text."
        )

    def is_coevolutionary(self) -> bool:
        return True

    # -- solver population: genome is a coding-agent system-prompt string -----
    def new_genome(self, llm: LLMClient) -> str:
        r = llm.chat(
            system=self.system_prompt(),
            user=("Write a concise, high-quality system prompt (<= 90 words) for a "
                  "Python bug-fixing agent. It must instruct the model to return the "
                  "corrected FULL file contents in a single ```python code block, make "
                  "the minimal change needed to pass the tests, and preserve the public "
                  "API. Return ONLY the prompt text."),
        )
        return r.text.strip() or (
            "You are an expert Python engineer. Return the corrected FULL file in one "
            "```python code block. Make the minimal change that makes the tests pass.")

    def mutate(self, llm: LLMClient, parent: str) -> str:
        r = llm.chat(
            system=self.system_prompt(),
            user=("Improve the following bug-fixing system prompt so it repairs more "
                  "Python bugs correctly across diverse cases. Change strategy, not just "
                  "wording. Keep it <= 90 words. Return ONLY the new prompt.\n\n"
                  f"CURRENT PROMPT:\n{parent}"),
        )
        return r.text.strip() or parent

    # -- adversary population: propose a breaking challenge ------------------
    def new_challenge(self, llm: LLMClient, target_genome: str) -> CodeChallenge | None:
        t0 = time.perf_counter()
        r = llm.chat(
            system=("You design adversarial Python bug-fix test cases. Produce a small "
                    "self-contained project: a buggy target module, a pytest test file "
                    "named test_target.py that fails on the bug, and a proven gold fix. "
                    "Return STRICT JSON with keys: task (str), target_file (str, the "
                    "buggy module filename), buggy_content (str), gold_content (str), "
                    "test_content (str, the pytest file), tags (list). The test must "
                    "import the target module by name. Do NOT include any command."),
            user=("Create ONE hard bug likely to defeat an agent using this system "
                  f"prompt:\n\n{target_genome}\n\nReturn ONLY JSON."),
        )
        self.timer.add_llm(time.perf_counter() - t0)
        try:
            txt = r.text
            txt = txt[txt.index("{"): txt.rindex("}") + 1]
            d = json.loads(txt)
            target_file = str(d["target_file"])
            # NOTE: any 'command'/'verify'/'cmd' key in the model output is ignored;
            # the verify command is the fixed module constant VERIFY_CMD.
            ch = CodeChallenge(
                task=str(d["task"]),
                files={target_file: str(d["buggy_content"]),
                       "test_target.py": str(d["test_content"])},
                target_file=target_file,
                gold_content=str(d["gold_content"]),
                tags=list(d.get("tags", [])),
            )
        except Exception:  # noqa: BLE001 - malformed proposals are simply rejected
            return None
        # Admission: the bug must be real (buggy fails) AND solvable (gold passes).
        buggy = ch.files[ch.target_file]
        tv = time.perf_counter()
        buggy_ok = run_verify(ch.files, ch.target_file, buggy)
        gold_ok = (not buggy_ok) and run_verify(ch.files, ch.target_file, ch.gold_content)
        self.timer.add_verify(time.perf_counter() - tv)
        if buggy_ok:
            return None   # buggy already passes -> vacuous challenge
        if not gold_ok:
            return None   # gold does not pass -> unsolvable / broken challenge
        return ch

    # -- behavior descriptor / cell -----------------------------------------
    def behavior(self, genome: str, eval_ctx: dict) -> tuple[float, ...]:
        words = genome.split()
        n = len(words)
        low = genome.lower()
        process = sum(low.count(w) for w in _PROCESS_WORDS)
        return (float(n), float(process))

    def cell(self, behavior: tuple[float, ...]) -> tuple[int, ...]:
        n, process = behavior
        return (
            lin_bin(n, 5, _PROMPT_LEN_MAX, self.len_bins),
            lin_bin(process, 0, 8, self.process_bins),
        )

    # -- shared evaluation kernel used by both fitness() and generality ------
    def score_challenges(self, genome: str, challenges: list["CodeChallenge"],
                         worker_llm: LLMClient) -> dict:
        """Evaluate genome against a list of challenges via sandboxed pytest.

        Returns {"accuracy": float, "n_challenges": int, "per_tag": dict}.
        This is the single place for per-challenge evaluation logic.
        """
        correct = 0
        per_tag: dict[str, list[int]] = {}
        for ch in challenges:
            current = ch.files.get(ch.target_file, "")
            user = (f"Task: {ch.task}\n\nFile `{ch.target_file}` current contents:\n"
                    f"```python\n{current}\n```\n\n"
                    "Return the corrected FULL contents of the file in a single "
                    "```python code block.")
            t0 = time.perf_counter()
            res = worker_llm.chat(system=genome, user=user)
            self.timer.add_llm(time.perf_counter() - t0)
            if not res.ok:
                hit = False  # LLM call failed; treat as miss, not correct
            else:
                t1 = time.perf_counter()
                hit = run_verify(ch.files, ch.target_file, extract_patch(res.text))
                self.timer.add_verify(time.perf_counter() - t1)
            correct += int(hit)
            for t in (ch.tags or ["untagged"]):
                per_tag.setdefault(t, []).append(int(hit))
        n = len(challenges)
        return {
            "accuracy": correct / n if n else 0.0,
            "n_challenges": n,
            "per_tag": {t: sum(v) / len(v) for t, v in per_tag.items()},
        }

    # -- fitness: exec-accuracy over opponent challenge-sets -----------------
    def fitness(self, genome: str, opponents: Sequence["CodeChallengeSet"], seed: int,
                worker_llm: LLMClient | None = None) -> tuple[float, tuple[float, ...], dict]:
        assert worker_llm is not None, "code_improvement needs a worker LLM to run the solver"
        challenges: list[CodeChallenge] = []
        for cs in opponents:
            challenges.extend(cs.challenges)
        if not challenges:
            challenges = list(self.seed_challenges)

        result = self.score_challenges(genome, challenges, worker_llm)
        beh = self.behavior(genome, {})
        meta = {"n_challenges": result["n_challenges"],
                "per_tag_acc": result["per_tag"]}
        return result["accuracy"], beh, meta

    def wrap_opponent(self, round_idx: int, challenges: list) -> "CodeChallengeSet":
        return CodeChallengeSet(round=round_idx, challenges=challenges)
