# CLAUDE.md — DRQ (Digital Red Queen)

## What this is

A port of Sakana AI's Digital Red Queen paper to a Text-to-SQL domain. It co-evolves two LLM-driven populations: **solver prompts** (system prompts that make a model produce correct SQL) and **adversarial challenges** (schema+question+gold_sql triples designed to break the current best solver). The outer Red Queen loop grows a history of opponents; the inner MAP-Elites loop maintains a quality-diverse archive of solvers. Rising held-out accuracy over rounds is the headline result.

## Repo layout

```
drq/                    Python package (the algorithm)
  engine.py             DRQ outer loop + MAP-Elites inner loop + threading
  archive.py            MAP-Elites archive: cells, fitness replacement, QD-score
  config.py             All configuration (LLMConfig, MapElitesConfig, DRQConfig)
  llm.py                Minimal OpenAI-compatible HTTP client; mock mode
  generality.py         Held-out evaluation — delegates to domain.score_challenges()
  domains/
    base.py             Domain Protocol (7 methods + optional hooks)
    text2sql.py         Concrete domain: SQL prompts vs adversarial DuckDB challenges
run.py                  CLI entry point (evolve | generality subcommands)
tests/                  pytest suite — 51 tests, all fast
```

## Commands

```bash
# Setup (one time)
python -m venv .venv && .venv/bin/pip install -e .[dev]

# Run tests
.venv/bin/pytest tests/ -v

# Offline smoke test (no model needed)
DRQ_LLM_MOCK=1 python run.py evolve --rounds 4 --iterations 6 --init-random 4 --batch 3 --out runs/smoke

# Real run against a local model (Ollama)
OPENAI_BASE_URL=http://localhost:11434/v1 DRQ_MODEL=qwen2.5-coder:32b \
  python run.py evolve --rounds 12 --out runs/sql1

# Measure generality of champions against held-out set
python run.py generality --champions runs/sql1/champions.json --out runs/sql1

# Lint + typecheck
.venv/bin/ruff check .
.venv/bin/mypy drq/ run.py --ignore-missing-imports --no-strict-optional
```

## Key CLI flags

```
evolve:
  --rounds INT            outer loop iterations (default 12)
  --iterations INT        MAP-Elites inner iterations per round (default 40)
  --init-random INT       random entities seeded at round start (default 8)
  --batch INT             mutation batch size per iteration (default 4)
  --workers INT           eval thread pool size (default 8)
  --history-k INT         0 = full opponent history; N = last N rounds only (default 0)
  --seed INT              RNG seed for reproducibility (default 0)
  --challenges-per-round  adversary target challenges per round (default 3)
  --token-budget INT      cumulative token ceiling; run halts cleanly when hit
                          (default 500_000_000 ~ runaway guard; 0 = unlimited;
                          lower to ~5_000_000 for a paid API)
  --evolver-model MODEL   override model for mutation/generation LLM
  --worker-model MODEL    override model for eval/scoring LLM (deterministic temp=0)
  --out DIR               output directory (run.jsonl, champions.json, opponents.json)

generality:
  --champions PATH        champions.json from an evolve run
  --heldout PATH          JSON file of Challenge objects; omit for built-in set
  --out DIR               writes generality.json
```

## Domain vocabulary (matches the paper)

| Term | Meaning in code |
|------|----------------|
| **genome** | The evolved entity — a prompt string for text2sql |
| **solver** | The genome population being evolved |
| **adversary / challenge** | A `(schema_sql, question, gold_sql)` triple that tries to break the solver |
| **opponent** | A `ChallengeSet` — all adversary challenges from one round |
| **champion** | The archive's best entity at the end of a round (`archive.best()`) |
| **fitness** | Execution-accuracy: fraction of challenges answered correctly by running SQL on DuckDB |
| **behavior descriptor** | `(prompt_length_words, reasoning_word_count)` — static, cheap diversity axes |
| **cell** | Discretized behavior bin — archive grid key |
| **generality** | Held-out accuracy (never seen during training); the paper's headline metric |
| **Red Queen effect** | Generality rising over rounds due to growing adversary history |

## Architecture decisions to preserve

- **Domain Protocol** (`drq/domains/base.py`): `engine.py` calls only protocol methods. Never add text2sql-specific imports to `engine.py` or `generality.py`.
- **Temperature binding**: `LLMClient` temperature is fixed at construction. Evolver uses `cfg.llm.temperature` (default 1.0); worker uses `.as_worker()` (temperature=0.0). Never pass `temperature=` to `chat()`.
- **`score_challenges()` is the single evaluation kernel**: Both `fitness()` and `generality()` delegate here. Do not add a second evaluation loop.
- **In-memory DuckDB only**: `_run()` always creates `duckdb.connect(":memory:")`. Each call is independent and transient.
- **Thread pool is IO-bound**: `_score_batch` uses `ThreadPoolExecutor`. If you port to a CPU-bound domain, switch to `multiprocessing.Pool` — the interface is identical.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENAI_BASE_URL` | `http://localhost:11434/v1` | LLM endpoint (Ollama default) |
| `OPENAI_API_KEY` | `ollama` | API key (use `ollama` for local) |
| `DRQ_MODEL` | `qwen2.5-coder:32b` | Model name |
| `DRQ_LLM_MOCK` | `0` | Set to `1` for offline testing (no network calls) |

## Output files (in `--out` dir)

| File | Content |
|------|---------|
| `run.jsonl` | One JSON line per round: fitness, QD-score, coverage, challenge tags, `timing` ({llm_s, verify_s, llm_calls, verify_calls} — where the round's wall-clock went; summed across the eval thread pool, so the ratio matters, not the absolute totals), `budget` ({tokens, calls, limit} — cumulative token spend and ceiling) |
| `champions.json` | Array of `{round, fitness, genome, cell}` — one per round |
| `opponents.json` | Full opponent history as serialized ChallengeSets |
| `generality.json` | Per-round `{generality, train_fitness, per_tag}` curve |

## Adding a new domain

1. Create `drq/domains/your_domain.py` implementing the `Domain` Protocol (7 methods in `base.py`).
2. Implement `score_challenges(genome, challenges, worker_llm) -> dict` — this is used by generality evaluation.
3. Implement `wrap_opponent(round_idx, challenges) -> Any` to create your opponent type.
4. If your domain is coevolutionary, implement `new_challenge(llm, target_genome)` and `is_coevolutionary() -> True`.
5. If not coevolutionary, leave `new_challenge` returning `None` and set `seed_challenges` to a fixed eval set.

## Known constraints

- **Mock mode fitness is always 0.0**: The mock worker always returns `SELECT COUNT(*) FROM orders;` which doesn't match any gold SQL. The loop still runs and produces valid structure — just don't interpret mock fitness values.
- **Gold SQL correctness**: The adversary admission check verifies gold SQL *executes* but not that it's *correct*. For production use, curate a trusted held-out set (Spider/BIRD) rather than relying on LLM-generated gold.
- **`domain.seed_challenges` is accessed via `getattr` with `[]` fallback**: If your domain has no seed challenges, return an empty list (or a non-empty default set) from `__init__`.
