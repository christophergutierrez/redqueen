# CONTEXT.md — Killhouse run: "improve this repo" (DRQ)

Objective (grilled): **make all concrete, verified improvements** surfaced by a multi-agent
discovery audit of the DRQ working tree at `/mnt/storage/git_home/redqueen`.

Grounded at: working tree on top of HEAD `5a655bb` with uncommitted timing + token-budget +
code_improvement work. Discovery: 2026-07-03, 5-agent read-only fan-out, all findings verified.

## Backlog (deduped, ranked, batched)

### M1 — text2sql fitness-signal correctness  [1 HIGH + 2 MED]
- `exec_match`/`_run` stringifies cells (`str(c)`): `Decimal('100.00')` != `100.0` → correct SQL scored wrong. FIX: numeric normalization/tolerance. GATE: `exec_match(s,"SELECT SUM(amount)","SELECT SUM(amount::DOUBLE)") is True`.
- Order-insensitive compare: both gold+pred `sorted()`, so ORDER BY never graded; docstring falsely claims otherwise. FIX: when gold has top-level ORDER BY, compare unsorted. GATE: reversed-ORDER-BY pred → False.
- `schema_sql.split(";")` breaks on `;` inside string literals. FIX: single `con.execute` (DuckDB multi-statement). GATE: INSERT with `';'` admits/executes.

### M2 — crash-hardening the run loop  [3 HIGH]
- `None` champion appended unconditionally → crash on `--init-random 0`. FIX: guard append/seed/dump. GATE: `--init-random 0` mock run completes.
- `chat()` returns `text=None` on `content:null` (finish_reason=length) with `ok=True` → `.strip()`/regex crash. FIX: coerce `content or ""`. GATE: null-content payload → `text==""`.
- `_score_batch` (`ex.map`+`list`) re-raises first exception → whole round/run aborts, no dump. FIX: try/except in `_score` → floor-fitness Entity. GATE: one raising genome in a batch → batch returns all N entities.

### M3 — determinism fix  [HIGH]
- `run.py` generality worker built from `cfg.llm` (temp 1.0) not `.as_worker()`. FIX: `.as_worker()`. GATE: worker temp == 0.0.

### M4 — sandbox env hardening (SECURITY)  [BLOCKING; mandatory gate]
- `run_verify` runs model-generated Python with `env=os.environ.copy()` → `OPENAI_API_KEY` exfiltrable (demonstrated: key written to disk). FIX: minimal child env (no secrets; PATH/PYTHONHASHSEED only), ideally `-I -S`/no-network. GATE: target reading `OPENAI_API_KEY` finds it absent from child env.

### M5 — mock fidelity  [HIGH]
- Mock branches on `"SQL" in system` → offline code_improvement evolves SQL-analyst prompts (see killhouse/redqueen-exec-prompt.md). FIX: optional Domain hook `mock_reply(system,user,role)`; llm.py consults a callable (stays domain-agnostic); code_improvement returns a valid ```python block. Preserve "mock fitness 0.0" contract. GATE: mock code_improvement champion genome has no "SQL analyst"; mock worker output `compile()`s.

### M6 — Protocol→ABC + opponent contract  [2 HIGH + 3 MED; large blast radius]
- base.py is a `Protocol`; default method bodies never execute, but CLAUDE.md tells authors they can omit methods → a 3rd domain following docs breaks. FIX: convert to ABC with real defaults.
- Undeclared engine coupling: `o.challenges`, `c.tags`, `o.to_dict()` (engine.py) not in Protocol. FIX: declare `Opponent` protocol + `summarize_opponent` hook.
- `is_coevolutionary()` consumed nowhere → non-coevolutionary domains duplicate opponents each round. FIX: branch in `_adversary_step`.
- `pop_timing` smuggles output via instance state (kernel impure); genome assumed JSON-serializable though typed `Any`. FIX: return timing from score_challenges / `genome_to_json` hook.
- Dissolves: getattr guards, vestigial `behavior(eval_ctx)`, dead worker `temperature=0.0` in run.py.

### M7 — test-coverage batch  [MED/LOW]
- Add unit tests: `_active_opponents` (history_k), `seed_with_champions` on/off, `evaluate_lineage` (+ code_improvement generality), adversary fallback-to-seeds, threaded timing + budget-overshoot bound, empty-archive `qd_score`/`coverage`. Decide `log_bin` (dead code): delete or adopt for length axis. Mark slow sandbox tests.

### M8 — docs sync  [LOW]
- README: `drq.py`→`engine.py`; add code_improvement/timing/budget to layout; add `--token-budget`, `pop_timing`. CLAUDE.md: add `--domain`; "51 tests"→87.

## Pre-empted (no action)
- pytest runtime dep already in `pyproject.toml` main dependencies (byte-identical to killhouse submodule).

## Recommendation
M1–M5 + M7 + M8 are high-value and contained. M6 is the keystone but large-blast-radius and will
trip ARCHITECTURE_DESIGN — do it last in checkpoint mode, or defer.
