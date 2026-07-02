"""Tests for the cumulative token budget and its clean-halt behavior."""
from drq.budget import TokenBudget
from drq.config import DRQConfig, LLMConfig, MapElitesConfig
from drq.domains.text2sql import Text2SQLDomain
from drq.engine import DRQ
from drq.llm import LLMClient


def test_budget_add_and_exceeded():
    b = TokenBudget(100)
    assert not b.exceeded()
    b.add(60)
    assert not b.exceeded()
    b.add(60)
    assert b.exceeded()
    snap = b.snapshot()
    assert snap["tokens"] == 120
    assert snap["calls"] == 2
    assert snap["limit"] == 100


def test_budget_unlimited_variants():
    for lim in (0, -5, None):
        b = TokenBudget(lim)
        b.add(10**9)
        assert b.exceeded() is False
        assert b.snapshot()["limit"] is None


def test_budget_ignores_negative_tokens():
    b = TokenBudget(100)
    b.add(-50)               # a bogus/absent usage must not decrement
    assert b.snapshot()["tokens"] == 0


def test_llm_client_accounts_tokens_in_mock():
    b = TokenBudget(10**9)
    c = LLMClient(LLMConfig(mock=True), budget=b)
    c.chat(system="a system prompt about SQL", user="please write a query")
    snap = b.snapshot()
    assert snap["calls"] == 1
    assert snap["tokens"] > 0


def test_llm_client_without_budget_is_noop():
    c = LLMClient(LLMConfig(mock=True))   # no budget attached
    r = c.chat(system="s", user="u")
    assert r.ok is True


def test_engine_halts_cleanly_on_budget(tmp_path):
    """A tiny budget is blown during round 0, so the loop halts before finishing
    all requested rounds — and still writes its output files."""
    cfg = DRQConfig(
        rounds=5,
        out_dir=str(tmp_path),
        token_budget=50,
        llm=LLMConfig(mock=True),
        me=MapElitesConfig(iterations=1, init_random=1, batch_size=1),
    )
    drq = DRQ(Text2SQLDomain(), cfg)
    champions = drq.run()
    assert drq._halted is True
    assert 0 < len(champions) < 5           # started, then stopped early
    assert (tmp_path / "champions.json").exists()
    assert (tmp_path / "opponents.json").exists()


def test_engine_completes_all_rounds_under_generous_budget(tmp_path):
    cfg = DRQConfig(
        rounds=3,
        out_dir=str(tmp_path),
        token_budget=0,                     # unlimited
        llm=LLMConfig(mock=True),
        me=MapElitesConfig(iterations=1, init_random=1, batch_size=1),
    )
    drq = DRQ(Text2SQLDomain(), cfg)
    champions = drq.run()
    assert drq._halted is False
    assert len(champions) == 3