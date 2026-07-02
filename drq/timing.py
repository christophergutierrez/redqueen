"""Thread-safe accumulator for per-round evaluation timing.

An evaluation is `LLM generates a candidate -> verify it` (run SQL on DuckDB, or
run pytest in a sandbox). This splits a round's wall-clock into the two costs so
`run.jsonl` can show where the seconds actually went — settling the recurring
"is the bottleneck the model or the verifier?" question with numbers.

Because evaluation is parallel (engine uses a ThreadPoolExecutor), the summed
seconds can EXCEED the round's wall time; the model-independent signal is the
RATIO llm_s : verify_s, not the absolute totals.
"""
from __future__ import annotations

import threading


class EvalTimer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._llm_s = 0.0
        self._verify_s = 0.0
        self._llm_calls = 0
        self._verify_calls = 0

    def add_llm(self, dt: float) -> None:
        with self._lock:
            self._llm_s += dt
            self._llm_calls += 1

    def add_verify(self, dt: float) -> None:
        with self._lock:
            self._verify_s += dt
            self._verify_calls += 1

    def pop(self) -> dict:
        """Return accumulated timing and reset to zero (called once per round)."""
        with self._lock:
            out = {
                "llm_s": round(self._llm_s, 3),
                "verify_s": round(self._verify_s, 3),
                "llm_calls": self._llm_calls,
                "verify_calls": self._verify_calls,
            }
            self._llm_s = self._verify_s = 0.0
            self._llm_calls = self._verify_calls = 0
            return out
