"""Text2SQL domain with genuine Red Queen co-evolution.

Two populations:
  SOLVER   — an LLM *system prompt* (the evolved entity). Given a schema + a
             natural-language question, the worker-LLM uses this prompt to emit
             SQL. Good prompts generalize across schemas and question styles.
  CHALLENGE — an adversarial (schema, question, gold_sql) triple. The adversary
             evolves challenges that the *current champion solver* gets wrong.

DRQ outer loop: each round the adversary produces a new champion CHALLENGE-SET
(the hardest questions it can find against the reigning solver). The solver
population must then evolve to answer ALL historical challenge-sets — a growing
set of opponents, exactly as in the paper. Robustness to "changing workloads and
schemas" is literally generality against held-out challenge-sets.

Fitness of a solver = fraction of challenge questions it answers correctly
(execution-accuracy: result set of predicted SQL == result set of gold SQL on a
throwaway DuckDB instance), averaged over the opponent challenge-sets.

Behavior descriptor (for MAP-Elites diversity):
  axis 0: prompt length in tokens (rough proxy: whitespace-split words)
  axis 1: "reasoning-ness" — does the prompt ask for step-by-step / CTE / planning?
These are cheap, static, and give a spread of qualitatively different prompts.
"""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

import duckdb

from ..archive import lin_bin
from ..llm import LLMClient
from ..timing import EvalTimer

# --------------------------------------------------------------------------- #
# Challenge representation                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class Challenge:
    schema_sql: str        # CREATE TABLE ... ; INSERT ... ; (self-contained DDL+data)
    question: str          # natural-language question
    gold_sql: str          # reference query producing the correct answer
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"schema_sql": self.schema_sql, "question": self.question,
                "gold_sql": self.gold_sql, "tags": self.tags}


# A seed challenge-set so round 0 has something to optimize against.
SEED_CHALLENGES: list[Challenge] = [
    Challenge(
        schema_sql=(
            "CREATE TABLE orders(id INTEGER, customer TEXT, amount DECIMAL(10,2), status TEXT);"
            "INSERT INTO orders VALUES "
            "(1,'acme',100.0,'paid'),(2,'acme',50.0,'refunded'),"
            "(3,'globex',200.0,'paid'),(4,'globex',NULL,'paid'),(5,'initech',75.0,'pending');"
        ),
        question="What is the total paid amount per customer, for customers with at least one paid order?",
        gold_sql=("SELECT customer, SUM(amount) AS total FROM orders "
                  "WHERE status='paid' GROUP BY customer ORDER BY customer;"),
        tags=["group_by", "filter", "null"],
    ),
    Challenge(
        schema_sql=(
            "CREATE TABLE emp(id INTEGER, name TEXT, dept TEXT, salary INTEGER, mgr INTEGER);"
            "INSERT INTO emp VALUES "
            "(1,'a','eng',120,NULL),(2,'b','eng',100,1),(3,'c','eng',100,1),"
            "(4,'d','sales',90,NULL),(5,'e','sales',95,4);"
        ),
        question="For each department, who earns the most? Return dept and name, ties broken alphabetically.",
        gold_sql=("SELECT dept, name FROM (SELECT dept, name, "
                  "ROW_NUMBER() OVER (PARTITION BY dept ORDER BY salary DESC, name ASC) rn "
                  "FROM emp) t WHERE rn=1 ORDER BY dept;"),
        tags=["window", "rank", "ties"],
    ),
]


# --------------------------------------------------------------------------- #
# Execution-accuracy evaluator                                                 #
# --------------------------------------------------------------------------- #


_NULL = "\x00NULL"  # sentinel so a SQL NULL never collides with the string 'None'

# DuckDB connection config used for ALL evaluations. external_access=False disables
# file/network I/O (read_csv, read_text, COPY TO file, extension autoloading) while
# leaving in-memory query execution intact — which is all the evaluator needs.
# Without this, adversary-authored SQL can read host files and write via COPY TO.
_DUCKDB_EVAL_CONFIG = {"enable_external_access": False}


def _norm_cell(c: Any) -> str:
    """Canonicalize a result cell so numerically-equal answers compare equal.

    DuckDB returns e.g. SUM over DECIMAL as ``Decimal('150.00')`` but the same
    logical value via DOUBLE as ``150.0``; without normalization a correct
    prediction is scored wrong. Integers keep exact string form (no float
    round-trip, so large IDs are safe); non-integer reals round to 6 places.
    """
    if c is None:
        return _NULL
    if isinstance(c, bool):
        # \x00-prefixed so a BOOLEAN true/false never collides with the TEXT
        # strings 'true'/'false' (which fall through to str(c) below).
        return "\x00T" if c else "\x00F"
    if isinstance(c, int):
        return str(c)
    if isinstance(c, (float, Decimal)):
        f = float(c)
        if not math.isfinite(f):
            # NaN/inf would raise in int(f); give them a stable sentinel so a
            # gold whose result is NaN still matches itself (and doesn't crash
            # _run, which would otherwise score it as a broken query).
            return f"\x00{f}"          # "\x00nan" | "\x00inf" | "\x00-inf"
        if f == int(f):
            return str(int(f))          # integer-valued (any magnitude): exact
        return format(round(f, 6), ".6f")
    return str(c)


_PAREN = re.compile(r"\([^()]*\)")
_ORDER_BY = re.compile(r"\border\s+by\b", re.IGNORECASE)
# String literals ('' escape), quoted identifiers ("" escape), and comments —
# blanked out before the ORDER BY scan so a literal like 'order by z' or a
# `-- order by` comment can't be mistaken for a real result-ordering clause.
_SQL_NOISE = re.compile(
    r"'(?:[^']|'')*'"        # single-quoted string
    r'|"(?:[^"]|"")*"'       # double-quoted identifier
    r"|--[^\n]*"             # line comment
    r"|/\*.*?\*/",           # block comment
    re.DOTALL,
)


def _has_top_level_order_by(sql: str) -> bool:
    """True iff `sql` has an ORDER BY outside any parentheses — i.e. one that
    orders the final result set, not an ORDER BY inside a window (`OVER (...)`)
    or a subquery. String/comment noise is blanked first, then parenthesized
    groups are stripped innermost-first.

    Known residual: a top-level statement that is *itself* fully wrapped in
    parens — `(SELECT ... ORDER BY ...)` — has its ORDER BY stripped and is
    treated as unordered. Rare in practice; a full SQL parse would be needed to
    close it and is deliberately out of scope for this cheap check."""
    sql = _SQL_NOISE.sub(" ", sql)
    prev = None
    while prev != sql:
        prev = sql
        sql = _PAREN.sub(" ", sql)
    return _ORDER_BY.search(sql) is not None


def _run(schema_sql: str, query: str) -> tuple[bool, Any]:
    """Execute query against a fresh in-memory DuckDB with the given schema.
    Returns (ok, rows_in_natural_order) with normalized cells (or (False, err)).
    Rows are NOT sorted here — ordering is decided by exec_match."""
    con = None
    try:
        con = duckdb.connect(":memory:", config=_DUCKDB_EVAL_CONFIG)
        if schema_sql.strip():
            # one execute() runs the whole multi-statement schema and does not
            # choke on ';' inside string literals (unlike a naive split)
            con.execute(schema_sql)
        rows = con.execute(query).fetchall()
        return True, [tuple(_norm_cell(c) for c in r) for r in rows]
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    finally:
        if con is not None:
            con.close()


def exec_match(schema_sql: str, gold_sql: str, pred_sql: str) -> bool:
    gold_ok, gold = _run(schema_sql, gold_sql)
    if not gold_ok:
        return False  # broken gold -> can't score; treat as miss
    pred_ok, pred = _run(schema_sql, pred_sql)
    if not pred_ok:
        return False
    if _has_top_level_order_by(gold_sql):
        return pred == gold           # gold orders the result -> order matters
    return sorted(gold) == sorted(pred)  # otherwise compare as a multiset


_SQL_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_sql(text: str) -> str:
    m = _SQL_FENCE.search(text)
    if m:
        return m.group(1).strip()
    # otherwise take from first SELECT/WITH to end
    m = re.search(r"\b(WITH|SELECT)\b", text, re.IGNORECASE)
    return text[m.start():].strip() if m else text.strip()


# --------------------------------------------------------------------------- #
# Domain                                                                       #
# --------------------------------------------------------------------------- #

_PROMPT_LEN_MAX = 120     # words; upper bound for BD normalization
_REASON_WORDS = ("step", "plan", "cte", "first", "think", "reason", "list", "restate")


class Text2SQLDomain:
    name = "text2sql"

    def __init__(self, seed_challenges: list[Challenge] | None = None,
                 len_bins: int = 5, reason_bins: int = 3):
        self.seed_challenges = seed_challenges or list(SEED_CHALLENGES)
        self.len_bins = len_bins
        self.reason_bins = reason_bins
        self.timer = EvalTimer()

    def pop_timing(self) -> dict:
        """Return and reset accumulated LLM vs verify timing for the last round."""
        return self.timer.pop()

    # -- LLM description -----------------------------------------------------
    def system_prompt(self) -> str:
        return (
            "You are evolving SYSTEM PROMPTS for a downstream Text-to-SQL agent. "
            "A good system prompt makes a language model reliably translate a natural-"
            "language question about a given SQL schema into a single correct DuckDB "
            "query. Prompts should generalize across schemas and correctly handle NULLs, "
            "joins, aggregation, window functions, and tie-breaking. Output ONLY the prompt text."
        )

    def is_coevolutionary(self) -> bool:
        return True

    # -- solver population: genome is a system-prompt string -----------------
    def new_genome(self, llm: LLMClient) -> str:
        r = llm.chat(
            system=self.system_prompt(),
            user=("Write a concise, high-quality system prompt (<= 90 words) for a "
                  "Text-to-SQL agent targeting DuckDB. It must instruct the model to "
                  "return ONLY the SQL. Make it robust to NULLs, joins, and ranking."),
        )
        return r.text.strip() or "You are a SQL expert. Return ONLY a valid DuckDB SQL query."

    def mutate(self, llm: LLMClient, parent: str) -> str:
        r = llm.chat(
            system=self.system_prompt(),
            user=("Improve the following Text-to-SQL system prompt so it produces more "
                  "correct DuckDB queries across diverse schemas. Change strategy, not just "
                  "wording. Keep it <= 90 words. Return ONLY the new prompt.\n\n"
                  f"CURRENT PROMPT:\n{parent}"),
        )
        return r.text.strip() or parent

    # -- adversary population: propose a breaking challenge ------------------
    def new_challenge(self, llm: LLMClient, target_genome: str) -> Challenge | None:
        t0 = time.perf_counter()
        r = llm.chat(
            system=("You design adversarial Text-to-SQL test cases. Produce a self-contained "
                    "DuckDB schema (CREATE TABLE + a few INSERTs), a natural-language question "
                    "that is easy for a human but tricky for an LLM (NULL handling, multi-join, "
                    "correlated subquery, tie-breaking, or date logic), and the correct gold SQL. "
                    "Return STRICT JSON with keys schema_sql, question, gold_sql, tags."),
            user=("Create ONE hard case likely to defeat an agent using this system prompt:\n\n"
                  f"{target_genome}\n\nReturn ONLY JSON."),
        )
        self.timer.add_llm(time.perf_counter() - t0)
        try:
            txt = r.text
            txt = txt[txt.index("{"): txt.rindex("}") + 1]
            d = json.loads(txt)
            ch = Challenge(schema_sql=d["schema_sql"], question=d["question"],
                           gold_sql=d["gold_sql"], tags=list(d.get("tags", [])))
            # validate gold executes before admitting it
            tv = time.perf_counter()
            ok, _ = _run(ch.schema_sql, ch.gold_sql)
            self.timer.add_verify(time.perf_counter() - tv)
            return ch if ok else None
        except Exception:  # noqa: BLE001
            return None

    # -- behavior descriptor / cell -----------------------------------------
    def behavior(self, genome: str, eval_ctx: dict) -> tuple[float, ...]:
        words = genome.split()
        n = len(words)
        low = genome.lower()
        reason = sum(low.count(w) for w in _REASON_WORDS)
        return (float(n), float(reason))

    def cell(self, behavior: tuple[float, ...]) -> tuple[int, ...]:
        n, reason = behavior
        return (
            lin_bin(n, 5, _PROMPT_LEN_MAX, self.len_bins),
            lin_bin(reason, 0, 6, self.reason_bins),
        )

    # -- shared evaluation kernel used by both fitness() and generality ------
    def score_challenges(self, genome: str, challenges: list["Challenge"],
                         worker_llm: LLMClient) -> dict:
        """Evaluate genome against a specific list of challenges.

        Returns {"accuracy": float, "n_challenges": int, "per_tag": dict}.
        This is the single place for per-challenge evaluation logic.
        """
        correct = 0
        per_tag: dict[str, list[int]] = {}
        for ch in challenges:
            user = (f"Schema:\n{ch.schema_sql}\n\nQuestion: {ch.question}\n\n"
                    "Return ONLY the DuckDB SQL query.")
            t0 = time.perf_counter()
            res = worker_llm.chat(system=genome, user=user)
            self.timer.add_llm(time.perf_counter() - t0)
            if not res.ok:
                hit = False  # LLM call failed; treat as miss, not correct
            else:
                t1 = time.perf_counter()
                hit = exec_match(ch.schema_sql, ch.gold_sql, extract_sql(res.text))
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
    def fitness(self, genome: str, opponents: Sequence["ChallengeSet"], seed: int,
                worker_llm: LLMClient | None = None) -> tuple[float, tuple[float, ...], dict]:
        """`opponents` is a list of ChallengeSet (each a champion from a prior round).
        Fitness = mean exec-accuracy across every challenge in every opponent set."""
        assert worker_llm is not None, "text2sql needs a worker LLM to run the evolved prompt"
        challenges: list[Challenge] = []
        for cs in opponents:
            challenges.extend(cs.challenges)
        if not challenges:
            challenges = list(self.seed_challenges)

        result = self.score_challenges(genome, challenges, worker_llm)
        beh = self.behavior(genome, {})
        meta = {"n_challenges": result["n_challenges"],
                "per_tag_acc": result["per_tag"]}
        return result["accuracy"], beh, meta

    def wrap_opponent(self, round_idx: int, challenges: list) -> "ChallengeSet":
        return ChallengeSet(round=round_idx, challenges=challenges)


@dataclass
class ChallengeSet:
    """A champion opponent: the set of hard challenges from one adversary round."""
    round: int
    challenges: list[Challenge]

    def to_dict(self) -> dict:
        return {"round": self.round, "challenges": [c.to_dict() for c in self.challenges]}
