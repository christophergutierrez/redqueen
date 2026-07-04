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

from .budget import TokenBudget
from .config import LLMConfig


@dataclass
class ChatResult:
    text: str
    ok: bool = True
    error: str = ""


class LLMClient:
    def __init__(self, cfg: LLMConfig, budget: "TokenBudget | None" = None,
                 role: str = "worker", mock_reply=None):
        self.cfg = cfg
        self._rng = random.Random(0)
        self.budget = budget
        self.role = role            # "evolver" | "worker" — passed to a domain mock hook
        self.mock_reply = mock_reply  # optional Callable(system, user, role) -> str | None

    def chat(self, system: str, user: str,
             max_tokens: int | None = None) -> ChatResult:
        if self.cfg.mock:
            res = self._mock(system, user)
            self._account(system, user, res, None)
            return res
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
            # content is null on finish_reason=length / tool-calls / filtered
            # completions; coerce to "" so downstream .strip()/regex never see None
            content = data["choices"][0]["message"].get("content") or ""
            res = ChatResult(text=content)
            self._account(system, user, res, data.get("usage"))
            return res
        except Exception as e:  # noqa: BLE001 - surface everything as a soft failure
            res = ChatResult(text="", ok=False, error=str(e))
            self._account(system, user, res, None)
            return res

    def _account(self, system: str, user: str, res: ChatResult,
                 usage: dict | None) -> None:
        """Charge this call against the shared token budget. Uses the API's
        reported usage when present, else a ~4-chars/token estimate over the
        prompt + completion (so mock/usage-less servers still accrue)."""
        if self.budget is None:
            return
        if usage and usage.get("total_tokens"):
            tok = int(usage["total_tokens"])
        else:
            tok = (len(system) + len(user) + len(res.text)) // 4
        self.budget.add(tok)

    # ------------------------------------------------------------------ mock
    def _mock(self, system: str, user: str) -> ChatResult:
        """Cheap deterministic-ish stand-in so the loop is testable offline.

        A domain may supply a faithful reply via `mock_reply` (kept domain-agnostic
        here — we just call the callable); otherwise fall back to the generic
        SQL-flavored stand-in below."""
        if self.mock_reply is not None:
            reply = self.mock_reply(system, user, self.role)
            if reply is not None:
                return ChatResult(text=reply)
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
