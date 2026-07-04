# Digital Red Queen (DRQ) — a runnable port

A faithful, minimal port of Sakana's [Digital Red Queen](https://pub.sakana.ai/drq/)
to a practical domain: **evolving Text-to-SQL system prompts that stay robust as
schemas and workloads change.**

The paper's one non-negotiable idea: the Red Queen effect only appears when you
optimize against a *growing history of adversaries*. Optimizing a prompt against
a fixed benchmark is the paper's single-round baseline — it produces brittle
specialists (each beats ~28% of held-out cases). So this port co-evolves two
populations instead of hill-climbing one.

```
drq/
  config.py            # all knobs (rounds, MAP-Elites, LLM endpoint)
  llm.py               # OpenAI-compatible client (vLLM/Ollama/llama.cpp) + mock mode
  archive.py           # MAP-Elites quality-diversity archive
  budget.py            # cumulative token-budget ceiling (clean halt)
  timing.py            # thread-safe LLM-vs-verify timing split
  engine.py            # outer Red Queen loop + inner MAP-Elites, threaded eval
  generality.py        # held-out generality curve (the real progress metric)
  domains/
    base.py            # Domain Protocol (swap in your own domain here)
    text2sql.py        # concrete domain: solver-prompts vs adversarial challenges
    code_improvement.py # concrete domain: coding-agent prompts vs bug-fix challenges
run.py                 # CLI: evolve | generality
```

## Quick start

```bash
# offline smoke test — no model needed, proves the loop + evaluator work
DRQ_LLM_MOCK=1 python run.py evolve --rounds 4 --iterations 6 --init-random 4 --batch 3

# real run against a local model (Ollama)
OPENAI_BASE_URL=http://localhost:11434/v1 DRQ_MODEL=qwen2.5-coder:32b \
  python run.py evolve --rounds 12 --out runs/sql1

# with token budget for paid APIs (e.g. 5M tokens)
OPENAI_BASE_URL=... DRQ_MODEL=... python run.py evolve --rounds 12 --token-budget 5000000 --out runs/sql1

# measure generality of the champion lineage against a held-out challenge set
python run.py generality --champions runs/sql1/champions.json --out runs/sql1
```

---

## Domains

The evolvable target is selected with `--domain` (default `text2sql`):

| `--domain` | Genome | Challenge | Objective fitness signal |
|------------|--------|-----------|--------------------------|
| `text2sql` | a SQL system prompt | `(schema, question, gold_sql)` | predicted SQL's result set matches gold on a throwaway DuckDB |
| `code_improvement` | a bug-fixing system prompt | a self-contained mini-project (buggy file + `test_target.py` + proven gold fix) | the worker's patched file makes a **fixed** `pytest` command exit 0 in an ephemeral sandbox |

### Running the `code_improvement` domain in mock mode

The `code_improvement` domain uses `pytest` as its runtime verifier, so install
the package normally or with dev tools before running it:

```bash
python -m pip install -e .[dev]
```

```bash
# offline smoke test — runs the full co-evolution loop with no model
DRQ_LLM_MOCK=1 python run.py evolve --domain code_improvement \
  --rounds 2 --iterations 3 --init-random 2 --batch 2 --out runs/ci_smoke

# generality of the champion lineage against the built-in held-out code set
python run.py generality --domain code_improvement \
  --champions runs/ci_smoke/champions.json --out runs/ci_smoke
```

As with `text2sql`, **mock-mode fitness is always 0.0**: the mock worker cannot
emit a valid Python patch, so no sandbox test passes. The loop structure still runs
and produces valid output files — mock mode validates plumbing, not fitness. For a
meaningful run, point `OPENAI_BASE_URL`/`DRQ_MODEL` at a real coding model.

**Safety.** This domain executes model-generated Python. Each evaluation runs in its
own `tempfile.TemporaryDirectory`; the verify command is a fixed module constant
(`VERIFY_CMD`) that is **never** taken from model output; written paths are confined
to the sandbox; and the subprocess has a hard timeout. Run it in a trusted local
environment.

---

## 1. System prompt template (domain description for the evolver)

`Text2SQLDomain.system_prompt()` frames the evolver's job — *it evolves prompts,
it does not answer questions*:

> You are evolving SYSTEM PROMPTS for a downstream Text-to-SQL agent. A good
> system prompt makes a language model reliably translate a natural-language
> question about a given SQL schema into a single correct DuckDB query. Prompts
> should generalize across schemas and correctly handle NULLs, joins,
> aggregation, window functions, and tie-breaking. Output ONLY the prompt text.

For a different domain, this is the one method you rewrite first: describe the
environment, the entity, and what "good" means, exactly as the paper gives the
LLM "a concise manual for the Redcode assembly language ... and an example."

## 2. New-entity and Mutate prompts

Both live in the domain (`new_genome`, `mutate`) and mirror the paper's split
("produce a novel program" vs "modify it in ways that could improve
performance"):

- **New:** *"Write a concise, high-quality system prompt (<= 90 words) for a
  Text-to-SQL agent targeting DuckDB. Return ONLY the SQL. Make it robust to
  NULLs, joins, ranking."*
- **Mutate:** *"Improve the following prompt so it produces more correct queries
  across diverse schemas. **Change strategy, not just wording.** Keep it <= 90
  words. Return ONLY the new prompt."*

The adversary has its own generator, `new_challenge`, which is the second
population: *"Create ONE hard case likely to defeat an agent using this system
prompt ... Return STRICT JSON {schema_sql, question, gold_sql, tags}."* Every
proposed gold query is executed before admission, so broken challenges are
dropped.

## 3. Loop structure

```
for t in range(T):                        # OUTER: Red Queen rounds
    C_t = adversary_evolve(champion)      #   new opponent challenge-set
    opponents.append(C_t)                 #   growing history {C_0..C_t}
    archive = MapElites()                 # INNER: quality-diversity
    seed archive with prior champions     #   (paper bootstraps like this)
    for _ in range(init_random):          #   fresh random entities
        archive.add(score(new_genome()))
    for _ in range(iterations):
        parents  = [archive.sample() for _ in batch]
        children = [mutate(p) for p in parents]
        for e in score_batch(children):   #   threaded: eval is IO-bound
            archive.add(e)                #   insert iff beats cell incumbent
    champion = archive.best()
    champions.append(champion)
```

Evaluation is parallelized with a `ThreadPoolExecutor` because scoring a solver
means making LLM calls (IO-bound). If you port DRQ to a **CPU-bound simulator**
(a real Core War VM, a physics sim), switch `_score_batch` to
`multiprocessing.Pool` — the interface is identical, only the executor changes.

## 4. Evaluation and selection

- **Fitness** = execution-accuracy: run the solver's SQL and the gold SQL on a
  throwaway in-memory DuckDB and compare *result sets*, not strings. This
  correctly credits semantically-equivalent queries (verified in tests).
- Fitness is averaged over **every challenge in every active opponent set** —
  this is what makes later rounds harder and drives generality.
- **Selection** is MAP-Elites: an offspring only displaces the incumbent in its
  own behavior cell. Behavior descriptor = (prompt length, "reasoning-ness").
  This preserves diversity the way the paper's (threads, memory-coverage) axes do
  — the ablation in the paper shows collapsing to a single cell hurts, especially
  in later rounds.

## 5. Measuring progress

- **Generality** (`generality.py`): fraction of a **held-out** challenge set the
  champion answers correctly. Score every round's champion against the *same*
  held-out set → a generality curve. Rising = the Red Queen effect is real, not
  overfitting to the adversary's latest tricks. This is the paper's headline
  metric.
- **QD-score / coverage**: sum of elite fitnesses and number of filled cells,
  logged every round — inner-loop health.
- **Phenotype convergence** (paper's second finding): run N independent seeds,
  represent each champion as its vector of per-held-out-challenge scores, and
  track cross-run variance over rounds. Decreasing variance = convergence toward
  a general strategy. Genotype (prompt embedding) variance staying flat = the
  same convergent-evolution dissociation the paper reports.
- **Cyclic dynamics**: build the round-champion vs round-champion win matrix and
  count rock-paper-scissors triples; full history (`--history-k 0`) should show
  far fewer cycles than `--history-k 1`.

## 6. Adaptations for your setup (Arch, local LLMs)

**Serving the model.** vLLM gives the best throughput for the many small,
parallel eval calls DRQ makes:

```bash
# Arch: python-vllm is in the AUR, or use a venv
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen2.5-Coder-32B-Instruct --port 8000 --max-model-len 8192
export OPENAI_BASE_URL=http://localhost:8000/v1 DRQ_MODEL=Qwen/Qwen2.5-Coder-32B-Instruct
```

On the DGX Spark (GB10, 128GB unified) a 32B coder at bf16/8-bit fits
comfortably and the unified memory is ideal for the concurrent worker calls.
Bump `--workers` to match how many concurrent requests your server handles well
(vLLM batches them; start at 16). Ollama works too but caps concurrency lower —
set `OLLAMA_NUM_PARALLEL` and keep `--workers` modest.

**Split evolver vs worker models.** The paper found bigger models didn't help
much as the *operator*. You can point the evolver at a strong instruct model and
the worker at a fast coder model by instantiating two `LLMConfig`s — cheap win on
throughput since the worker does most of the calls.

**Determinism.** Worker runs at `temperature=0` so fitness is stable across
re-evaluation; the evolver runs hot (`1.0`) for exploration. Keep it that way or
your archive will churn.

**Extending to a new domain.** Implement the `Domain` Protocol in
`domains/base.py` (7 methods). If your domain is genuinely adversarial (two
agents), set `is_coevolutionary()` and implement `new_challenge`. If it's a
single-population QD task, leave the co-evolution hooks as no-ops and treat
"opponents" as a fixed eval set — but know you're then running the *baseline*,
not full DRQ.

## Honest caveats

- Paper-faithful, but their result is a **statistical trend over ~96 runs**, weak
  per-run. Expect the generality curve to be noisy; run multiple seeds and
  aggregate before concluding anything.
- **text2sql evaluation safety.** `_run()` executes adversary-authored SQL in an
  in-memory DuckDB with `enable_external_access=False`, which blocks file/network
  I/O (read_csv, COPY TO file, extension autoloading). Without this flag, a
  crafted `gold_sql` can read host files (`/etc/passwd`, `~/.aws/credentials`)
  or write via `COPY TO`. The flag is set at the module level in `_DUCKDB_EVAL_CONFIG`
  and applied to every evaluation connection.
- LLM-authored gold SQL can be subtly wrong even when it executes. The admission
  check catches *broken* gold, not *incorrect* gold. For production use, curate a
  trusted held-out set (Spider/BIRD schemas are a good source) rather than
  trusting the adversary's gold for final generality numbers.
- Exec-accuracy folds ORDER BY correctness in only when the gold depends on it;
  tighten `_run` if strict ordering matters for you.
