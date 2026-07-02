"""DRQ configuration.

All knobs in one place. Tuned defaults are conservative so a full run
finishes in reasonable time against a local model.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class LLMConfig:
    """OpenAI-compatible endpoint. Works with vLLM, Ollama, llama.cpp server,
    LM Studio, or hosted APIs. Set DRQ_LLM_MOCK=1 for a no-network dry run."""
    base_url: str = os.environ.get("OPENAI_BASE_URL", "http://localhost:11434/v1")  # Ollama default
    api_key: str = os.environ.get("OPENAI_API_KEY", "ollama")
    model: str = os.environ.get("DRQ_MODEL", "qwen2.5-coder:32b")
    temperature: float = 1.0          # high temp for the *evolver* (mutation operator)
    worker_temperature: float = 0.0   # deterministic *worker* (the entity being scored)
    max_tokens: int = 1200
    timeout_s: float = 120.0
    mock: bool = os.environ.get("DRQ_LLM_MOCK", "0") == "1"


@dataclass
class MapElitesConfig:
    iterations: int = 40          # inner-loop iterations per round
    init_random: int = 8          # fresh entities generated at round start
    batch_size: int = 4           # candidates proposed per iteration
    seed_with_champions: bool = True  # bootstrap archive with prior champions (paper does this)


@dataclass
class DRQConfig:
    rounds: int = 12              # outer loop (T in the paper)
    history_k: int = 0            # 0 = full history (paper's "full DRQ"); else last K champions
    eval_workers: int = 8         # thread pool for IO-bound LLM eval calls
    seed: int = 0
    out_dir: str = "runs/default"
    llm: LLMConfig = field(default_factory=LLMConfig)
    me: MapElitesConfig = field(default_factory=MapElitesConfig)
    # Override evolver or worker model independently (None falls back to llm)
    evolver_llm: LLMConfig | None = None
    worker_llm: LLMConfig | None = None
    challenges_per_round: int = 3      # target number of adversary challenges per round
