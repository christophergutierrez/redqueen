"""Minimal OpenAI-compatible chat client (stdlib only, no SDK dependency).

Two roles, per the DRQ paper's split:
  - the EVOLVER: high-temperature model used as the generation/mutation operator
  - the WORKER: the model that *executes* an evolved entity (e.g. runs an
    evolved prompt to produce SQL). Deterministic (temp=0) so fitness is stable.

Mock mode (DRQ_LLM_MOCK=1) lets the whole pipeline run offline for testing.
"""
from __future__ import annotations

import json
import random
import urllib.request
from dataclasses import dataclass

from .config import LLMConfig


@dataclass
class ChatResult:
    text: str
    ok: bool = True
    error: str = ""


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._rng = random.Random(0)

    def chat(self, system: str, user: str,
             max_tokens: int | None = None) -> ChatResult:
        if self.cfg.mock:
            return self._mock(system, user)
        payload = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        req = urllib.request.Request(
            self.cfg.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
                data = json.loads(resp.read())
            return ChatResult(text=data["choices"][0]["message"]["content"])
        except Exception as e:  # noqa: BLE001 - surface everything as a soft failure
            return ChatResult(text="", ok=False, error=str(e))

    # ------------------------------------------------------------------ mock
    def _mock(self, system: str, user: str) -> ChatResult:
        """Cheap deterministic-ish stand-in so the loop is testable offline."""
        if "Return ONLY a SQL query" in system or "SQL" in system[:200]:
            # worker mock: emit a plausible query
            return ChatResult(text="SELECT COUNT(*) FROM orders;")
        # evolver mock: emit a random prompt-entity
        tricks = [
            "Think step by step about which tables are needed before writing SQL.",
            "Always alias tables. Prefer explicit JOIN ... ON over implicit joins.",
            "First restate the question, list candidate columns, then write one query.",
            "Use CTEs to structure multi-step aggregations.",
            "Check for NULLs in join keys and filter them explicitly.",
            "Prefer window functions over self-joins for ranking questions.",
        ]
        k = self._rng.randint(2, 4)
        body = " ".join(self._rng.sample(tricks, k))
        return ChatResult(text=f"You are an expert SQL analyst. {body} Return ONLY the SQL, no prose.")
