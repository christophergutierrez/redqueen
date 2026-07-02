"""Cumulative token budget — a runaway/cost guard for a whole evolution run.

The loop is already count-bounded (rounds x iterations x batch), but those knobs
can still imply tens of millions of tokens, and a misconfiguration or a model
that emits huge outputs could balloon that. `TokenBudget` is a shared, thread-safe
ceiling: every LLM call adds its token usage, and the engine checks `exceeded()`
at loop boundaries to halt cleanly (finalizing outputs) rather than burning on.

A limit of 0 (or None / negative) means unlimited.
"""
from __future__ import annotations

import threading


class TokenBudget:
    def __init__(self, limit: int | None):
        # 0 / None / negative -> unlimited
        self.limit: int | None = limit if (limit and limit > 0) else None
        self._lock = threading.Lock()
        self.used = 0
        self.calls = 0

    def add(self, tokens: int) -> None:
        with self._lock:
            self.used += max(0, int(tokens))
            self.calls += 1

    def exceeded(self) -> bool:
        if self.limit is None:
            return False
        with self._lock:
            return self.used >= self.limit

    def snapshot(self) -> dict:
        with self._lock:
            return {"tokens": self.used, "calls": self.calls, "limit": self.limit}
