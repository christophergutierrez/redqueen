# Implementation Plan: DRQ Codebase Improvement

## Planning Verdict
- **verdict**: READY_WITH_ASSUMPTIONS
- **task_tier**: standard
- **tier_trigger**: Multi-file refactor with limited removal (top-level duplicates), no external consumers, no persisted data, no CI/deploy/security surfaces. Standard is the correct tier.
- **reason**: Five phases: structural cleanup, domain decoupling, LLM split config, configurability, test suite. All gates are falsifiable. One staleness assumption (no VCS; snapshot-based).

---

## Repository State (Staleness Contract)

- **HEAD**: none — not a git repository at time of discovery
- **snapshot**: file state as of 2026-07-01
- **dirty_files**: n/a
- **discovered_at**: 2026-07-01T00:00:00Z
- **existing user changes to preserve**: all files under `drq/drq/` are the canonical implementation; top-level `drq.py`, `text2sql.py`, `README.md` are pre-refactor duplicates

**Executor staleness check**: Before starting, verify file hashes or mtimes match the facts below. If any cited file changed, re-run its citation and re-validate the affected milestone before proceeding.

---

## Repository Findings

Citations: `fact <- command -> relevant output`

| id | fact | citation |
|----|------|----------|
| F1 | `redqueen/drq.py` (145 lines) is byte-identical to `drq/drq/drq.py` | `diff drq.py drq/drq/drq.py` → no diff |
| F2 | `redqueen/text2sql.py` (256 lines) is byte-identical to `drq/drq/domains/text2sql.py` | `diff text2sql.py drq/drq/domains/text2sql.py` → no diff |
| F3 | `redqueen/README.md` is byte-identical to `drq/README.md` | `diff README.md drq/README.md` → no diff |
| F4 | `drq/drq/drq.py` line 28 imports `ChallengeSet, Text2SQLDomain` from `.domains.text2sql` — couples the domain-agnostic engine to one domain | `grep -n "ChallengeSet\|Text2SQLDomain" drq/drq/drq.py` → line 28 |
| F5 | `DRQ.__init__` instantiates both evolver and worker from the same `cfg.llm` — the documented split-model feature is not implemented | `read drq/drq/drq.py:37-38` → `self.evolver = LLMClient(cfg.llm)` / `self.worker = LLMClient(cfg.llm)` |
| F6 | `domains/base.py` Protocol `fitness` signature takes 3 params (genome, opponents, seed); `Text2SQLDomain.fitness` takes 4 (adds `worker_llm`) — Protocol does not match implementation | `read drq/drq/domains/base.py:435` vs `drq/drq/domains/text2sql.py:667` |
| F7 | `generality.py` imports `Challenge, Text2SQLDomain, exec_match, extract_sql` from text2sql domain, but the `domain: Text2SQLDomain` parameter in `generality()` is never used — dead param, tight coupling | `read drq/drq/generality.py:8,362` |
| F8 | `_adversary_step` in `drq.py` hardcodes `n_want = 3` — not configurable | `read drq/drq/drq.py:65` → `n_want = 3` |
| F9 | Mock in `llm.py` checks `"SQL" in system[:200]` — domain-specific logic inside the generic LLM client | `read drq/drq/llm.py:250` |
| F10 | No test files anywhere | `find drq -name "test_*.py"` → empty |
| F11 | No `pyproject.toml` or `setup.py` | `ls drq/pyproject.toml drq/setup.py` → No such file |
| F12 | No `.gitignore` | `find . -name ".gitignore"` → empty |
| F13 | `HELDOUT` in `run.py` is hardcoded (2 challenges); no `--heldout` file flag; no `--seed` CLI arg | `read drq/run.py:730-795` |
| F14 | `run.py` imports work when executed from `drq/` dir as `python run.py` — `from drq.config import ...` resolves because `drq/` (the package) is a sibling | `read drq/run.py:724-727` |

**Pre-existing baseline**: `DRQ_LLM_MOCK=1 python drq/run.py evolve --rounds 2 --iterations 4 --init-random 3 --batch 2 --out /tmp/drq_base` exits 0 (smoke test passes before any changes).

**Known risk**: mock in `llm.py` (F9) returns a fixed SQL string regardless of schema; exec_match may report false hits. This pre-exists and is not introduced by this plan.

---

## Requested Outcomes & Non-Goals

| id | outcome | type |
|----|---------|------|
| O1 | Top-level `drq.py`, `text2sql.py`, duplicate `README.md` deleted | explicit |
| O2 | Canonical package layout: `redqueen/drq/` is the Python package, `run.py` at root, one `README.md` at root | implied |
| O3 | `.gitignore` suppresses `runs/`, `__pycache__`, `.venv`, `*.egg-info`, `dist/` | implied |
| O4 | `pyproject.toml` enables `pip install -e .` from `redqueen/` | implied |
| O5 | `drq/drq.py` has no text2sql-specific imports; engine is domain-agnostic | explicit |
| O6 | `domains/base.py` Protocol `fitness` signature matches `Text2SQLDomain.fitness` | explicit |
| O7 | `generality.py` has no unused `domain` parameter; text2sql-specific imports kept but minimal | implied |
| O8 | `DRQConfig` has `evolver_llm: LLMConfig | None` and `worker_llm: LLMConfig | None` overrides | explicit |
| O9 | `DRQ.__init__` uses `cfg.evolver_llm or cfg.llm` and `cfg.worker_llm or cfg.llm` | implied |
| O10 | `DRQConfig` has `challenges_per_round: int = 3` (was hardcoded `n_want`) | explicit |
| O11 | `run.py evolve` accepts `--seed INT` | explicit |
| O12 | `run.py generality` accepts `--heldout PATH` to load challenges from JSON; falls back to builtin if omitted | explicit |
| O13 | `pytest tests/` passes: archive unit tests, text2sql unit tests, mock smoke integration test | explicit |
| O14 | Git repo initialized at `redqueen/` with initial commit | explicit |

**Non-goals**:
- Resume/checkpoint from mid-run
- New domain implementations
- Async concurrency model
- Non-DuckDB SQL dialects
- Plotting/visualization
- Reworking the mock to be domain-agnostic (F9 is a known wart, not fixed here)

---

## Outcome Traceability Matrix

| outcome_id | outcome | milestone_id(s) | invariant_id(s) | final_check |
|-----------|---------|-----------------|-----------------|-------------|
| O1 | Remove top-level duplicates | M1 | INV-1, INV-2, INV-3 | `ls redqueen/drq.py` → No such file |
| O2 | Canonical layout | M1 | INV-4 | `python -c "import drq"` from `redqueen/` works |
| O3 | .gitignore | M1 | INV-5 | `grep runs/ .gitignore` matches |
| O4 | pyproject.toml | M1 | INV-6 | `pip install -e . --dry-run` exits 0 |
| O5 | Engine domain-agnostic | M2 | INV-7 | `grep "from .domains.text2sql import" drq/drq.py` → no match |
| O6 | Protocol match | M2 | INV-8 | `grep "worker_llm" drq/domains/base.py` → present |
| O7 | generality.py minimal coupling | M2 | INV-9 | `generality()` signature has no `domain` param |
| O8 | evolver/worker split config | M3 | INV-10 | `grep "evolver_llm\|worker_llm" drq/config.py` → present |
| O9 | DRQ wires split config | M3 | INV-10 | `grep "evolver_llm or" drq/drq.py` → present |
| O10 | challenges_per_round config | M4 | INV-11 | `grep "challenges_per_round" drq/config.py` → present |
| O11 | --seed CLI | M4 | INV-11 | `python run.py evolve --help` → `--seed` listed |
| O12 | --heldout CLI | M4 | INV-11 | `python run.py generality --help` → `--heldout` listed |
| O13 | Test suite passes | M5 | INV-12 | `pytest tests/ -v` → all pass |
| O14 | Git initialized | M1 | (none; post-plan) | `git log --oneline` shows initial commit |

---

## Final-State Invariants

```yaml
- id: INV-1
  statement: redqueen/drq.py does not exist
  category: absence
  check: "test -f drq.py && echo FAIL || echo PASS"
  baseline_polarity: FAIL (file exists at discovery)
  post_condition: PASS
  failure_reasoning: top-level duplicate was not deleted
  scope: every-pass
  cost: cheap
  rationale: O1

- id: INV-2
  statement: redqueen/text2sql.py does not exist
  category: absence
  check: "test -f text2sql.py && echo FAIL || echo PASS"
  baseline_polarity: FAIL (file exists at discovery)
  post_condition: PASS
  failure_reasoning: top-level duplicate was not deleted
  scope: every-pass
  cost: cheap
  rationale: O1

- id: INV-3
  statement: duplicate README at drq/README.md does not exist after consolidation
  category: absence
  check: "test -f drq/README.md && echo FAIL || echo PASS"
  baseline_polarity: FAIL (file exists inside drq/ wrapper dir)
  post_condition: PASS
  failure_reasoning: nested README was not removed after package flattening
  scope: phase-end
  cost: cheap
  rationale: O2

- id: INV-4
  statement: drq/ is importable as a Python package from redqueen/
  category: presence
  check: "python -c 'import drq; print(drq.__file__)'"
  baseline_polarity: ImportError (package not on path without cd drq/)
  post_condition: prints drq/__init__.py path
  failure_reasoning: package layout broken or pyproject.toml wrong
  scope: every-pass
  cost: cheap
  rationale: O2

- id: INV-5
  statement: .gitignore contains 'runs/' entry
  category: presence
  check: "grep -q 'runs/' .gitignore && echo PASS || echo FAIL"
  baseline_polarity: FAIL (no .gitignore at discovery)
  post_condition: PASS
  failure_reasoning: .gitignore not created or missing runs/ line
  scope: phase-end
  cost: cheap
  rationale: O3

- id: INV-6
  statement: pip install dry-run succeeds
  category: presence
  check: "pip install -e . --dry-run 2>&1 | tail -1"
  baseline_polarity: error (no pyproject.toml)
  post_condition: "Successfully installed drq..." or "Would install..."
  failure_reasoning: pyproject.toml missing or malformed
  scope: phase-end
  cost: cheap
  rationale: O4

- id: INV-7
  statement: drq/drq.py contains no import from .domains.text2sql
  category: absence
  check: "grep -c 'from .domains.text2sql import' drq/drq.py"
  baseline_polarity: 1 (line 28 in current code)
  post_condition: 0
  failure_reasoning: domain coupling not removed
  scope: every-pass
  cost: cheap
  rationale: O5

- id: INV-8
  statement: domains/base.py Protocol fitness signature includes worker_llm parameter
  category: presence
  check: "grep -c 'worker_llm' drq/domains/base.py"
  baseline_polarity: 0 (absent at discovery)
  post_condition: >=1
  failure_reasoning: Protocol mismatch not fixed
  scope: phase-end
  cost: cheap
  rationale: O6

- id: INV-9
  statement: generality() function signature does not include a domain parameter
  category: absence
  check: "grep -A2 'def generality' drq/generality.py | grep -c 'domain'"
  baseline_polarity: 1 (domain: Text2SQLDomain present)
  post_condition: 0
  failure_reasoning: dead parameter not removed
  scope: phase-end
  cost: cheap
  rationale: O7

- id: INV-10
  statement: DRQConfig has evolver_llm and worker_llm fields
  category: presence
  check: "grep -c 'evolver_llm\|worker_llm' drq/config.py"
  baseline_polarity: 0 (absent at discovery)
  post_condition: >=2
  failure_reasoning: split config not added
  scope: phase-end
  cost: cheap
  rationale: O8

- id: INV-11
  statement: challenges_per_round in DRQConfig, --seed in evolve args, --heldout in generality args
  category: presence
  check: "grep -c 'challenges_per_round' drq/config.py && python run.py evolve --help | grep -c seed && python run.py generality --help | grep -c heldout"
  baseline_polarity: all 0
  post_condition: all >=1
  failure_reasoning: configurability not added
  scope: phase-end
  cost: cheap
  rationale: O10, O11, O12

- id: INV-12
  statement: pytest tests/ passes with no failures
  category: presence
  check: "pytest tests/ -v --tb=short 2>&1 | tail -5"
  baseline_polarity: error (no tests/ directory)
  post_condition: X passed, 0 failed
  failure_reasoning: test suite not created or logic broken
  scope: final
  cost: cheap
  rationale: O13
```

**Cheap every-pass subset**: INV-1, INV-2, INV-4, INV-7
**Full suite at phase-end / final**: all invariants

---

## Phased Plan

### Phase 1: Structural Cleanup

**objective**: Establish the canonical layout, remove all duplication, add packaging and VCS.
**prerequisites**: None
**files/components**: `redqueen/drq.py`, `redqueen/text2sql.py`, `redqueen/README.md`, `drq/drq/` (becomes `drq/`), `drq/run.py`, `drq/requirements.txt`, `drq/README.md` — plus new `pyproject.toml`, `.gitignore`
**blast_radius**: Local only. No external consumers. Deleting the three duplicate files is irreversible but they are byte-identical to the authoritative copies, confirmed (F1, F2, F3).
**rollback_boundary**: Restore `drq.py`, `text2sql.py`, `README.md` from `drq/drq/drq.py`, `drq/drq/domains/text2sql.py`, `drq/README.md`.

#### Milestone M1: canonical-layout

**outcome**: `redqueen/` contains exactly one README, one package (`drq/`), one CLI (`run.py`), packaging files, and a git repo with initial commit.

**implementation_scope**:
1. Delete `redqueen/drq.py` (byte-identical to `drq/drq/drq.py`, F1)
2. Delete `redqueen/text2sql.py` (byte-identical to `drq/drq/domains/text2sql.py`, F2)
3. Delete `redqueen/README.md` (byte-identical to `drq/README.md`, F3) — then move `drq/README.md` to `redqueen/README.md`
4. Move `drq/drq/` (the Python package dir) to `redqueen/drq/` — this is the package rename from `drq/drq/` to `redqueen/drq/`
5. Move `drq/run.py` to `redqueen/run.py`
6. Move `drq/requirements.txt` to `redqueen/requirements.txt`
7. Remove now-empty `drq/` wrapper directory
8. Create `redqueen/pyproject.toml`:
   ```toml
   [build-system]
   requires = ["setuptools>=68"]
   build-backend = "setuptools.backends.legacy:build"
   
   [project]
   name = "drq"
   version = "0.1.0"
   requires-python = ">=3.10"
   dependencies = ["duckdb>=1.0"]
   
   [project.scripts]
   drq = "run:main"
   ```
9. Create `redqueen/.gitignore`:
   ```
   runs/
   __pycache__/
   *.py[cod]
   .venv/
   venv/
   *.egg-info/
   dist/
   build/
   .pytest_cache/
   ```
10. `git init && git add -A && git commit -m "initial commit: drq package layout"`

**dependencies**: none

**acceptance_gates**:
- `test -f drq.py && echo FAIL || echo PASS` → PASS (INV-1; baseline: FAIL)
- `test -f text2sql.py && echo FAIL || echo PASS` → PASS (INV-2; baseline: FAIL)
- `python -c "import drq; print(drq.__file__)"` from `redqueen/` → prints `redqueen/drq/__init__.py` (INV-4; baseline: ImportError)
- `grep -q 'runs/' .gitignore && echo PASS || echo FAIL` → PASS (INV-5; baseline: FAIL)
- `pip install -e . --dry-run 2>&1 | grep -i "successfully\|would"` → match (INV-6; baseline: no match)
- `DRQ_LLM_MOCK=1 python run.py evolve --rounds 2 --iterations 4 --init-random 3 --batch 2 --out /tmp/m1_test` → exits 0 (regression; baseline: passes from drq/ dir)

**gate_failure_reasoning**: If INV-4 fails, the move of the package directory did not land at the right level, or `run.py` imports broke. Check `sys.path` and that `drq/__init__.py` exists at `redqueen/drq/__init__.py`.

**rollback_unit**: Restore the three deleted files from git stash or from the package copies; reverse the moves.

**stop_conditions**: If `run.py` imports cannot be made to resolve from `redqueen/`, add a `src/` layout with `src/drq/` and adjust `pyproject.toml` accordingly before continuing to Phase 2.

---

### Phase 2: Domain Decoupling

**objective**: The DRQ engine and generality evaluator have no text2sql-specific imports. The Protocol signature matches the implementation.
**prerequisites**: M1 complete (canonical layout)
**files/components**: `drq/drq.py`, `drq/domains/base.py`, `drq/generality.py`
**blast_radius**: Internal refactor only. No user-visible behavior change.
**rollback_boundary**: Revert the three files.

#### Milestone M2: decouple-engine

**outcome**: `drq/drq.py` uses only domain-agnostic types; `domains/base.py` Protocol matches `Text2SQLDomain`; `generality.py` dead parameter removed.

**implementation_scope**:

**drq/drq.py**:
- Remove `from .domains.text2sql import ChallengeSet, Text2SQLDomain` (line 28)
- Change `opponents: list[ChallengeSet]` to `opponents: list[Any]`
- Change `_adversary_step` return annotation from `ChallengeSet` to `Any`
- Change `_active_opponents` return annotation from `list[ChallengeSet]` to `list[Any]`
- In `_dump_final`, opponents are serialized via `o.to_dict()` — keep as-is (duck typing; `ChallengeSet` provides `to_dict()`)

**drq/domains/base.py**:
- Add `worker_llm: "LLMClient | None" = None` parameter to the `fitness` method signature in the Protocol, matching `Text2SQLDomain.fitness`

**drq/generality.py**:
- Remove `Text2SQLDomain` from imports (it was only used as a dead type annotation)
- Remove the `domain: Text2SQLDomain` parameter from `generality()` and `evaluate_lineage()` (unused, F7)
- Update callers in `run.py` accordingly (remove `domain` arg from the `evaluate_lineage` call)

**dependencies**: M1

**acceptance_gates**:
- `grep -c "from .domains.text2sql import" drq/drq.py` → 0 (INV-7; baseline: 1)
- `grep -c "worker_llm" drq/domains/base.py` → >=1 (INV-8; baseline: 0)
- `grep -A2 "def generality" drq/generality.py | grep -c "domain"` → 0 (INV-9; baseline: 1)
- `DRQ_LLM_MOCK=1 python run.py evolve --rounds 2 --iterations 4 --init-random 3 --batch 2 --out /tmp/m2_test` → exits 0 (regression)
- `python run.py generality --champions /tmp/m2_test/champions.json --out /tmp/m2_test` → exits 0 (regression)

**rollback_unit**: Revert `drq/drq.py`, `drq/domains/base.py`, `drq/generality.py`.

---

### Phase 3: Split LLM Config

**objective**: `DRQConfig` supports independent evolver and worker LLM configs, delivering the feature the README documents (F5).
**prerequisites**: M2 complete
**files/components**: `drq/config.py`, `drq/drq.py`, `run.py`
**blast_radius**: Backward-compatible: `evolver_llm` and `worker_llm` are `None` by default, falling back to the existing `llm` field.
**rollback_boundary**: Revert the three files.

#### Milestone M3: llm-split-config

**outcome**: Users can point the evolver at a stronger model and the worker at a faster one, matching the README.

**implementation_scope**:

**drq/config.py**:
- Add to `DRQConfig`:
  ```python
  evolver_llm: LLMConfig | None = None  # if None, falls back to llm
  worker_llm: LLMConfig | None = None   # if None, falls back to llm
  ```

**drq/drq.py**:
- Change constructor:
  ```python
  self.evolver = LLMClient(cfg.evolver_llm or cfg.llm)
  self.worker  = LLMClient(cfg.worker_llm  or cfg.llm)
  ```

**run.py** (`cmd_evolve`):
- Add `--evolver-model STR` and `--worker-model STR` optional flags
- If provided, construct a `LLMConfig` override for each:
  ```python
  evolver_llm = LLMConfig(model=args.evolver_model) if args.evolver_model else None
  worker_llm  = LLMConfig(model=args.worker_model,  temperature=0.0) if args.worker_model else None
  cfg = DRQConfig(..., evolver_llm=evolver_llm, worker_llm=worker_llm)
  ```

**dependencies**: M2

**acceptance_gates**:
- `grep -c "evolver_llm\|worker_llm" drq/config.py` → >=2 (INV-10; baseline: 0)
- `python run.py evolve --help | grep -c "evolver-model\|worker-model"` → 2 (presence; baseline: 0)
- `DRQ_LLM_MOCK=1 python run.py evolve --rounds 2 --iterations 4 --init-random 3 --batch 2 --out /tmp/m3_test` → exits 0 (regression; default path unchanged)
- `DRQ_LLM_MOCK=1 python run.py evolve --rounds 1 --iterations 2 --init-random 2 --batch 2 --evolver-model mock-big --worker-model mock-fast --out /tmp/m3_split_test` → exits 0 (split path)

**rollback_unit**: Revert `drq/config.py`, `drq/drq.py`, `run.py`.

---

### Phase 4: Configurability

**objective**: Remove hardcoded magic numbers; add `--seed` and `--heldout` CLI flags.
**prerequisites**: M3 complete
**files/components**: `drq/config.py`, `drq/drq.py`, `run.py`
**blast_radius**: New config field + new CLI flags. Fully backward-compatible; existing CLI invocations unchanged.

#### Milestone M4: config-knobs

**outcome**: `challenges_per_round` configurable; `--seed` and `--heldout` flags available in CLI.

**implementation_scope**:

**drq/config.py** — add to `DRQConfig`:
```python
challenges_per_round: int = 3   # adversary target (was n_want = 3 in drq.py)
```

**drq/drq.py** — `_adversary_step`:
- Replace `n_want = 3` with `n_want = self.cfg.challenges_per_round`

**run.py**:
- `evolve` subcommand: add `--seed INT` (default 0), wire to `DRQConfig(seed=args.seed, ...)`
- `generality` subcommand: add `--heldout PATH` (optional); if provided, load challenges from JSON file:
  ```python
  if args.heldout:
      with open(args.heldout) as f:
          raw = json.load(f)
      heldout = [Challenge(**c) for c in raw]
  else:
      heldout = HELDOUT
  ```

**dependencies**: M3

**acceptance_gates**:
- `grep -c "challenges_per_round" drq/config.py` → >=1 (INV-11 part 1; baseline: 0)
- `python run.py evolve --help | grep -c "\-\-seed"` → 1 (INV-11 part 2; baseline: 0)
- `python run.py generality --help | grep -c "\-\-heldout"` → 1 (INV-11 part 3; baseline: 0)
- `DRQ_LLM_MOCK=1 python run.py evolve --rounds 2 --iterations 4 --init-random 3 --batch 2 --seed 42 --out /tmp/m4_test` → exits 0

**rollback_unit**: Revert `drq/config.py`, `drq/drq.py`, `run.py`.

---

### Phase 5: Test Suite

**objective**: Pytest suite covers archive logic, text2sql evaluators, and the full mock loop.
**prerequisites**: M4 complete (or M1; tests can be written against the stabilized layout)
**files/components**: new `tests/` directory; `tests/test_archive.py`, `tests/test_text2sql.py`, `tests/test_smoke.py`, `pyproject.toml` (add pytest config)

#### Milestone M5: test-suite

**outcome**: `pytest tests/ -v` passes with zero failures.

**implementation_scope**:

**tests/test_archive.py** — unit tests for `drq.archive`:
- `test_add_better_replaces_incumbent`: add entity to empty grid, then add better entity to same cell → only better survives
- `test_add_worse_keeps_incumbent`: add entity, then add worse to same cell → incumbent unchanged
- `test_best_empty`: `MapElites.best()` on empty archive → None
- `test_best_nonempty`: best() returns highest-fitness entity
- `test_coverage_and_qd_score`: 3 entities in 3 cells → coverage=3, qd_score=sum of fitnesses
- `test_lin_bin_clamp`: lin_bin with value below lo → 0; above hi → n_bins-1

**tests/test_text2sql.py** — unit tests for `drq.domains.text2sql`:
- `test_exec_match_correct`: gold and pred produce same result set → True
- `test_exec_match_wrong_result`: pred returns wrong rows → False
- `test_exec_match_broken_pred`: pred SQL is invalid → False
- `test_exec_match_broken_gold`: gold SQL is invalid → False (can't score)
- `test_extract_sql_fence`: markdown ```sql block → extracts inner SQL
- `test_extract_sql_plain`: plain SELECT ... → returned as-is
- `test_extract_sql_with_text`: "The answer is SELECT ..." → extracts from SELECT onward
- `test_challenge_serialization`: `Challenge.to_dict()` roundtrips through `Challenge(**d)`

**tests/test_smoke.py** — integration test (requires no model; uses mock):
- `test_mock_evolve_runs`: `DRQ_LLM_MOCK=1 python run.py evolve --rounds 2 --iterations 4 --init-random 3 --batch 2 --out <tmp>` → exits 0, produces `champions.json` and `run.jsonl`
- `test_mock_generality_runs`: use champions.json from above → `python run.py generality --champions ... --out <tmp>` exits 0, produces `generality.json`
- `test_champions_json_structure`: champions.json has `round`, `fitness`, `genome`, `cell` fields for each entry

**pyproject.toml** — add:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
```

**dependencies**: M1 (layout stable)

**acceptance_gates**:
- `pytest tests/ -v --tb=short` → exits 0, all tests pass (INV-12; baseline: error — no tests/ dir)
- `pytest tests/test_archive.py -v` → passes (presence gate for archive coverage)
- `pytest tests/test_text2sql.py -v` → passes (presence gate for evaluator coverage)
- `pytest tests/test_smoke.py -v` → passes (integration gate)

**rollback_unit**: Delete `tests/` directory. No production code changed in this milestone.

---

## Subagent Matrix

| Work item | Role | Tier | Parallelizable | Inputs | Required output |
|-----------|------|------|----------------|--------|-----------------|
| M1: file ops + git init | Implementer | standard | no (sequential with M2-M5) | file list, pyproject.toml contents above | passing INV-1,2,4,5,6 + smoke test |
| M2: domain decouple | Implementer | standard | no | M1 output, F4/F6/F7 findings | passing INV-7,8,9 + smoke tests |
| M3: LLM split | Implementer | standard | no | M2 output | passing INV-10 + split path smoke test |
| M4: config knobs | Implementer | standard | no | M3 output | passing INV-11 |
| M5: tests | Implementer | standard | can start after M1 | stable layout from M1 | passing INV-12 |

M5 can be parallelized against M2-M4 since it only requires the stable package layout from M1, not the Phase 2-4 changes (tests can be written against the existing interface and updated as changes land).

---

## Consolidated Verification

Run after M5 (or whichever is last):

```bash
# 1. No duplicates (INV-1, INV-2)
test -f drq.py    && echo "FAIL: drq.py still exists"    || echo "PASS: drq.py gone"
test -f text2sql.py && echo "FAIL: text2sql.py still exists" || echo "PASS: text2sql.py gone"

# 2. Package importable (INV-4)
python -c "import drq; print('PASS:', drq.__file__)"

# 3. .gitignore has runs/ (INV-5)
grep -q 'runs/' .gitignore && echo "PASS: .gitignore ok" || echo "FAIL: runs/ missing from .gitignore"

# 4. pyproject.toml installs (INV-6)
pip install -e . --dry-run 2>&1 | grep -i "successfully\|would" && echo "PASS" || echo "FAIL"

# 5. No engine/domain coupling (INV-7)
grep "from .domains.text2sql import" drq/drq.py && echo "FAIL: coupling remains" || echo "PASS: decoupled"

# 6. Protocol match (INV-8)
grep -q "worker_llm" drq/domains/base.py && echo "PASS: Protocol fixed" || echo "FAIL"

# 7. generality() no dead param (INV-9)
grep -A3 "def generality" drq/generality.py | grep -q "domain" && echo "FAIL: dead param remains" || echo "PASS"

# 8. Split config (INV-10)
grep -q "evolver_llm" drq/config.py && grep -q "worker_llm" drq/config.py && echo "PASS" || echo "FAIL"

# 9. New CLI flags (INV-11)
python run.py evolve --help | grep -q "\-\-seed"       && echo "PASS: --seed"     || echo "FAIL"
python run.py generality --help | grep -q "\-\-heldout" && echo "PASS: --heldout" || echo "FAIL"

# 10. Test suite (INV-12)
pytest tests/ -v --tb=short

# 11. End-to-end smoke (regression)
DRQ_LLM_MOCK=1 python run.py evolve --rounds 3 --iterations 6 --init-random 4 --batch 3 --seed 1 --out /tmp/drq_final_verify
python run.py generality --champions /tmp/drq_final_verify/champions.json --out /tmp/drq_final_verify
echo "PASS: full pipeline"
```

---

## Falsification

### Cold-Start Walk

A fresh agent reading only this plan and the repository would encounter these gaps:

1. **Move ambiguity (M1, step 4)**: "Move `drq/drq/` to `redqueen/drq/`" — the outer `drq/` directory is the wrapper, the inner `drq/drq/` is the package. A fresh agent might move the outer wrapper. **Fix already in plan**: the implementation scope says "Move `drq/drq/` (the Python package dir) to `redqueen/drq/`" — but should also say "and delete the now-empty `drq/` wrapper".

2. **run.py import resolution (M1)**: After moving package from `drq/drq/` to `redqueen/drq/`, `run.py` currently says `from drq.config import ...`. This works when run.py is at `redqueen/` and `drq/` is a sibling (Python finds it via `sys.path` with `.` included). The stop condition captures the fallback but a fresh agent may not know to check `sys.path`. **Mitigation**: the smoke test gate in M1 catches this immediately.

3. **`to_dict()` coupling in _dump_final (M2)**: `drq/drq.py`'s `_dump_final` calls `o.to_dict()` on opponents. After removing the `ChallengeSet` import, this still works (duck typing), but the plan should state explicitly that `to_dict()` is expected on any opponent type and `ChallengeSet` already provides it. No plan change needed; just noting the intent.

4. **`--evolver-model` LLMConfig partial override (M3)**: constructing `LLMConfig(model=args.evolver_model)` uses the dataclass default for `base_url`, `api_key`, etc. — so it will pick up env vars via `os.environ.get(...)` in the default values. This is correct behavior but a fresh agent might wonder if the override inherits the full environment. **Clarification added**: the implementation note now says env vars are picked up by default field values.

### Pre-Mortem

| failure mode | smallest fix |
|---|---|
| M1 smoke test fails because `run.py` can't find `drq` package | Add `sys.path.insert(0, os.path.dirname(__file__))` to `run.py`, or use `pip install -e .` as prerequisite for smoke test |
| M2 removes `ChallengeSet` import but `drq.py` type annotations still reference it via `opponents` field | Change `opponents: list[ChallengeSet]` → `opponents: list[Any]` — already specified in M2 |
| M3 `--evolver-model` constructs `LLMConfig` with wrong base_url if not set via env | LLMConfig defaults already use `os.environ.get("OPENAI_BASE_URL", ...)` — no issue |
| M5 smoke test is flaky if `/tmp/drq_final_verify` already exists from a prior run | Use `tempfile.mkdtemp()` in the test, not a hardcoded path |

---

## Adversarial Gate Audit *(inline; gate_audit: inline)*

Adopting the Gate Auditor persona: reviewing every gate for vacuousness, polarity, and coverage gaps.

**INV-1, INV-2** (absence of deleted files): Baseline FAIL confirmed by discovery. Concrete failure = file not deleted. Gate is non-vacuous. ✓

**INV-4** (package importable): Baseline `ImportError` confirmed (package currently not on path from `redqueen/`). Concrete failure = wrong directory structure after M1. Gate is non-vacuous. ✓

**INV-7** (`grep` absence): Baseline `1` (line 28 exists). The grep targets the exact import string. Could it pass while coupling remains? Only if the import is rewritten differently (e.g., importing from a re-export). The plan specifies removing the import entirely, so this is unlikely. ✓

**INV-8** (Protocol has `worker_llm`): Baseline `0`. Could this pass while the Protocol is still broken? If `worker_llm` is added as a comment rather than a parameter, the grep would match. **Tighten check**: `python -c "import inspect; from drq.domains.base import Domain; print(inspect.signature(Domain.fitness))"` should show `worker_llm` in the signature. Updating INV-8 check to the import-based version is recommended but not blocking.

**INV-9** (dead `domain` param removed): Baseline `1`. Concrete failure = parameter not removed. Non-vacuous. ✓

**INV-12** (pytest passes): Baseline error (no tests). Could tests pass vacuously? If test files exist but contain no test functions, pytest exits 0 with "no tests ran". **Mitigation**: M5 specifies 7+ test functions by name; the gate should add `--tb=short -q` and check the pass count is `>= 10` rather than just `exits 0`. Consider: `pytest tests/ --tb=short | grep -E "passed" | grep -v "^0 passed"`.

**Finding from audit**:
- INV-8 check is weaker than ideal (grep matches comments). Recommend tightening to Python import check. Severity: Minor.
- INV-12 gate could pass vacuously with empty test file. Recommend minimum-count check. Severity: Minor.

Both are Minor and do not block. The executor should apply the tightened checks.

---

## Replan Triggers

- If `run.py` imports cannot be resolved after M1 with a simple `sys.path` fix or `pip install -e .`, replan the package layout (possibly `src/drq/` layout).
- If `DRQ_LLM_MOCK=1` smoke test exposes a broken mock after M2 (domain decoupling changed calling convention), add characterization test for mock behavior before continuing to M3.
- If `duckdb` is unavailable in the test environment, `test_text2sql.py` exec tests will error — add a pytest skip marker and note it as a dependency gap.
- If the Protocol enforcement (PEP 544 `@runtime_checkable`) causes unexpected `isinstance` failures after M2, remove `@runtime_checkable` and keep Protocol as a documentation-only hint.

---

## Downstream Handoff

**Staleness check (run first)**:
```bash
# Verify files match discovery state before any edits:
diff <(wc -l drq.py drq/drq/drq.py) <(echo "145 drq.py" && echo "145 drq/drq/drq.py")  
# If any cited file was modified, re-run its citation command and check for drift.
```

**Ordered execution**:
1. M1 (structural cleanup + git init) — no dependencies
2. M2 (domain decouple) — requires M1
3. M3 (LLM split) — requires M2
4. M4 (config knobs) — requires M3
5. M5 (test suite) — requires M1; can run concurrently with M2-M4

**Cheap every-pass invariants** (run after each milestone): INV-1, INV-2, INV-4, INV-7

**Human confirmation points**: None. No blast-radius triggers apply (local repo, no external consumers).

---

## Review Record

**Reviewer passes** (inline, sequential):

**Gate Quality lens**: INV-8 check is grep-based and could be satisfied by a comment. Recommend Python-level check. Accepted as Minor. INV-12 vacuousness risk noted. Accepted as Minor with executor-level mitigation.

**Completeness lens**: M1 stop condition added for import resolution failure. `to_dict()` duck-typing expectation stated. HELDOUT load path in M4 is fully specified.

**Migration/Removal lens**: Three files deleted are confirmed byte-identical to authoritative copies (F1, F2, F3). Absence invariants INV-1/2 cover them. No characterization needed before deletion since content is 100% preserved in the package.

**Sequencing lens**: M5 can start after M1; explicitly noted. M2→M3→M4 chain is correct (each builds on the prior API surface).

**Risk & Rollback lens**: Each milestone has an explicit rollback unit. Phase 1 is the only irreversible step (file deletions), mitigated by confirming byte-identity first and git initialization at end of M1.

**Repository Alignment lens**: README promises split LLM config (F5); M3 delivers it. README loop structure description matches implementation; no doc changes needed.

**Simplification lens**: Plan is minimal per outcome. No abstractions added beyond what's needed (e.g., no plugin system, no config file parsing, no extra CLI subcommands). Accepted.

**Conflicts**: None. All lenses are compatible.

**Gate Audit findings**: INV-8 tighten (Minor), INV-12 tighten (Minor). Both accepted for executor to apply.

---

```json
{
  "verdict": "READY_WITH_ASSUMPTIONS",
  "task_tier": "standard",
  "tier_trigger": "multi-file refactor with limited removal, no external consumers, no blast-radius surface",
  "passes": 1,
  "open_blocking_findings": 0,
  "open_material_findings": 0,
  "vacuous_gates_found": 0,
  "cold_start_gaps": 1,
  "uncited_facts": 0,
  "gate_audit": "inline",
  "staleness": {
    "head": "none (not a git repo)",
    "dirty_files": [],
    "discovered_at": "2026-07-01T00:00:00Z"
  },
  "traceability_complete": true,
  "orphan_milestones": [],
  "characterization_gaps": [],
  "conflicts_resolved": [],
  "invariants": [
    { "id": "INV-1", "category": "absence", "scope": "every-pass", "cost": "cheap", "check": "test -f drq.py && echo FAIL || echo PASS", "baseline_polarity": "FAIL", "evidence": "diff drq.py drq/drq/drq.py -> no diff (F1)" },
    { "id": "INV-2", "category": "absence", "scope": "every-pass", "cost": "cheap", "check": "test -f text2sql.py && echo FAIL || echo PASS", "baseline_polarity": "FAIL", "evidence": "diff text2sql.py drq/drq/domains/text2sql.py -> no diff (F2)" },
    { "id": "INV-3", "category": "absence", "scope": "phase-end", "cost": "cheap", "check": "test -f drq/README.md && echo FAIL || echo PASS", "baseline_polarity": "FAIL", "evidence": "diff README.md drq/README.md -> no diff (F3)" },
    { "id": "INV-4", "category": "presence", "scope": "every-pass", "cost": "cheap", "check": "python -c 'import drq; print(drq.__file__)'", "baseline_polarity": "ImportError", "evidence": "package only resolvable from drq/ dir at discovery (F14)" },
    { "id": "INV-5", "category": "presence", "scope": "phase-end", "cost": "cheap", "check": "grep -q 'runs/' .gitignore && echo PASS || echo FAIL", "baseline_polarity": "FAIL", "evidence": "find . -name .gitignore -> empty (F12)" },
    { "id": "INV-6", "category": "presence", "scope": "phase-end", "cost": "cheap", "check": "pip install -e . --dry-run", "baseline_polarity": "error: no pyproject.toml", "evidence": "ls drq/pyproject.toml -> No such file (F11)" },
    { "id": "INV-7", "category": "absence", "scope": "every-pass", "cost": "cheap", "check": "grep -c 'from .domains.text2sql import' drq/drq.py", "baseline_polarity": "1", "evidence": "grep -n ChallengeSet drq/drq/drq.py -> line 28 (F4)" },
    { "id": "INV-8", "category": "presence", "scope": "phase-end", "cost": "cheap", "check": "grep -c 'worker_llm' drq/domains/base.py", "baseline_polarity": "0", "evidence": "read drq/drq/domains/base.py:435 (F6)" },
    { "id": "INV-9", "category": "absence", "scope": "phase-end", "cost": "cheap", "check": "grep -A2 'def generality' drq/generality.py | grep -c domain", "baseline_polarity": "1", "evidence": "read drq/drq/generality.py:362 (F7)" },
    { "id": "INV-10", "category": "presence", "scope": "phase-end", "cost": "cheap", "check": "grep -c 'evolver_llm\\|worker_llm' drq/config.py", "baseline_polarity": "0", "evidence": "read drq/drq/drq.py:37-38 (F5)" },
    { "id": "INV-11", "category": "presence", "scope": "phase-end", "cost": "cheap", "check": "grep -c 'challenges_per_round' drq/config.py", "baseline_polarity": "0", "evidence": "read drq/drq/drq.py:65 (F8)" },
    { "id": "INV-12", "category": "presence", "scope": "final", "cost": "cheap", "check": "pytest tests/ --tb=short | grep 'passed'", "baseline_polarity": "error: no tests dir", "evidence": "find drq -name test_*.py -> empty (F10)" }
  ],
  "cheap_every_pass_invariants": ["INV-1", "INV-2", "INV-4", "INV-7"],
  "blast_radius_decisions": [],
  "human_decisions_required": [],
  "plan_location": "implementation-plan.md",
  "summary": "5-phase plan: M1 canonical layout (remove 3 duplicates, add packaging, git init), M2 domain decoupling (remove engine/text2sql coupling, fix Protocol), M3 split LLM config (deliver documented feature), M4 config knobs (n_want to config, --seed, --heldout), M5 test suite (archive + eval + smoke). All gates falsifiable. READY_WITH_ASSUMPTIONS: no VCS at discovery, staleness check required before execution."
}
```
