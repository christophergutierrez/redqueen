"""Engine + LLM crash-hardening tests (M2) + generality determinism (M3)."""
import json
import os
import sys
import types

from drq.archive import Entity
from drq.config import DRQConfig, LLMConfig, MapElitesConfig
from drq.domains.text2sql import Text2SQLDomain
from drq.engine import DRQ
from drq.llm import LLMClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# --------------------------------------------------------------------------- #
# Fix 1 — a None champion (empty archive) must not crash the run              #
# --------------------------------------------------------------------------- #


def test_run_survives_empty_archive(tmp_path):
    """--init-random 0 with no prior champions leaves every archive empty;
    the run must complete and still write its output files, not traceback."""
    cfg = DRQConfig(
        rounds=2, out_dir=str(tmp_path), token_budget=0,
        llm=LLMConfig(mock=True),
        me=MapElitesConfig(iterations=2, init_random=0, batch_size=2,
                           seed_with_champions=True),
    )
    champs = DRQ(Text2SQLDomain(), cfg).run()
    assert champs == []                              # nothing producible, no crash
    assert (tmp_path / "champions.json").exists()
    assert json.load(open(tmp_path / "champions.json")) == []
    assert (tmp_path / "opponents.json").exists()


# --------------------------------------------------------------------------- #
# Fix 2 — chat() must never return text=None on a null-content response       #
# --------------------------------------------------------------------------- #


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_chat_coerces_null_content(monkeypatch):
    payload = {"choices": [{"message": {"content": None}}],
               "usage": {"total_tokens": 5}}
    monkeypatch.setattr("drq.llm.urllib.request.urlopen",
                        lambda *a, **k: _FakeResp(payload))
    r = LLMClient(LLMConfig(mock=False)).chat(system="s", user="u")
    assert r.ok is True
    assert r.text == ""            # never None -> downstream .strip()/regex safe


# --------------------------------------------------------------------------- #
# Fix 3 — one scoring failure must not abort the batch/run                    #
# --------------------------------------------------------------------------- #


class _RaisingDomain:
    """Minimal domain whose fitness raises for one specific genome."""
    name = "raising"
    seed_challenges: list = []

    def behavior(self, genome, eval_ctx):
        return (float(len(genome)), 0.0)

    def cell(self, behavior):
        return (int(behavior[0]) % 5, 0)

    def fitness(self, genome, opponents, seed, worker_llm=None):
        if genome == "boom":
            raise RuntimeError("kaboom")
        return 1.0, self.behavior(genome, {}), {}


def test_score_batch_isolates_failures(tmp_path):
    cfg = DRQConfig(out_dir=str(tmp_path), llm=LLMConfig(mock=True))
    drq = DRQ(_RaisingDomain(), cfg)
    ents = drq._score_batch(["ok1", "boom", "ok2"])
    assert len(ents) == 3                            # nothing dropped
    by_genome = {e.genome: e for e in ents}
    assert by_genome["boom"].fitness == 0.0          # failure scored as a miss
    assert "error" in by_genome["boom"].meta
    assert by_genome["ok1"].fitness == 1.0           # neighbors unaffected
    assert by_genome["ok2"].fitness == 1.0


# --------------------------------------------------------------------------- #
# M3 — generality must score with the deterministic worker (temp 0.0)         #
# --------------------------------------------------------------------------- #


def test_generality_scores_with_worker_temperature(monkeypatch, tmp_path):
    """cmd_generality must build its LLMClient via .as_worker() (temp 0.0), so
    the headline curve matches how train_fitness was evaluated."""
    import run

    captured = {}
    real_client = run.LLMClient

    def spy(cfg, *a, **k):
        captured["temperature"] = cfg.temperature
        return real_client(cfg, *a, **k)

    monkeypatch.setattr(run, "LLMClient", spy)
    monkeypatch.setenv("DRQ_LLM_MOCK", "1")
    champ = tmp_path / "champions.json"
    champ.write_text(json.dumps([{"round": 0, "fitness": 0.0,
                                  "genome": "g", "cell": [0, 0]}]))
    args = types.SimpleNamespace(domain="text2sql", champions=str(champ),
                                 out=str(tmp_path), heldout=None, token_budget=0)
    run.cmd_generality(args)
    assert captured["temperature"] == 0.0


# --------------------------------------------------------------------------- #
# M6 — domain contract: install-once opponents, structured genomes, Opponent  #
# --------------------------------------------------------------------------- #


class _FixedDomain:
    """Non-coevolutionary domain: a fixed seed set installed ONCE, never evolved.
    Omits every optional hook (is_coevolutionary, new_challenge, wrap_opponent,
    summarize_opponent, genome_to_json, pop_timing) so the engine's getattr
    defaults are what's under test."""
    name = "fixed"

    def __init__(self):
        self.seed_challenges = [
            types.SimpleNamespace(tags=["a"], to_dict=lambda: {"tags": ["a"]}),
        ]

    def system_prompt(self):
        return "solve"

    def new_genome(self, llm):
        return "genome"

    def mutate(self, llm, parent):
        return parent + "!"

    def behavior(self, genome, eval_ctx):
        return (float(len(genome)), 0.0)

    def cell(self, behavior):
        return (int(behavior[0]) % 4, 0)

    def fitness(self, genome, opponents, seed, worker_llm=None):
        return 1.0, self.behavior(genome, {}), {}

    def score_challenges(self, genome, challenges, worker_llm=None):
        return {"accuracy": 1.0, "n_challenges": len(challenges), "per_tag": {}}


def test_noncoevolutionary_installs_opponents_once(tmp_path):
    """A non-coevolutionary domain must NOT accumulate a duplicate opponent per
    round — the fixed seed set is installed exactly once (round 0)."""
    cfg = DRQConfig(
        rounds=3, out_dir=str(tmp_path), token_budget=0,
        llm=LLMConfig(mock=True),
        me=MapElitesConfig(iterations=1, init_random=2, batch_size=2),
    )
    drq = DRQ(_FixedDomain(), cfg)
    drq.run()
    assert len(drq.opponents) == 1                   # installed once, not per-round
    assert drq._coevolutionary is False


def test_default_opponent_satisfies_protocol(tmp_path):
    """The engine's fallback opponent (used when a domain omits wrap_opponent)
    must conform to the Opponent protocol the engine + serialization rely on."""
    from drq.domains.base import Opponent

    cfg = DRQConfig(
        rounds=2, out_dir=str(tmp_path), token_budget=0,
        llm=LLMConfig(mock=True),
        me=MapElitesConfig(iterations=1, init_random=2, batch_size=2),
    )
    drq = DRQ(_FixedDomain(), cfg)
    drq.run()
    opp = drq.opponents[0]
    assert isinstance(opp, Opponent)                 # round, challenges, to_dict()
    assert opp.round == 0
    assert opp.to_dict()["round"] == 0
    # opponents.json round-trips via to_dict()
    dumped = json.load(open(tmp_path / "opponents.json"))
    assert dumped[0]["round"] == 0


class _StructuredGenomeDomain(_FixedDomain):
    """A domain whose genome is a dict — exercises genome_to_json/from_json so
    champions.json holds JSON-serializable structure, not a repr."""
    name = "structured"

    def new_genome(self, llm):
        return {"prompt": "solve", "v": 1}

    def mutate(self, llm, parent):
        return {**parent, "v": parent["v"] + 1}

    def behavior(self, genome, eval_ctx):
        return (float(len(genome["prompt"])), 0.0)

    def genome_to_json(self, genome):
        return genome                                # already JSON-safe

    def genome_from_json(self, raw):
        return raw


def test_structured_genome_dumps_as_json(tmp_path):
    """champions.json must store the structured genome as JSON (a dict), routed
    through genome_to_json rather than str()."""
    cfg = DRQConfig(
        rounds=1, out_dir=str(tmp_path), token_budget=0,
        llm=LLMConfig(mock=True),
        me=MapElitesConfig(iterations=1, init_random=2, batch_size=2),
    )
    DRQ(_StructuredGenomeDomain(), cfg).run()
    champs = json.load(open(tmp_path / "champions.json"))
    assert champs                                    # at least one champion
    assert isinstance(champs[0]["genome"], dict)     # structure preserved
    assert champs[0]["genome"]["prompt"] == "solve"


# --------------------------------------------------------------------------- #
# M7 — coverage: champion re-seeding, adversary fallback, budget halt, curve   #
# --------------------------------------------------------------------------- #


def test_seed_with_champions_reinserts_prior_champions(tmp_path):
    """With seed_with_champions=True, _solver_step re-scores prior champions into
    the fresh archive so a good genome from an earlier round survives forward.
    The champion's equal-fitness sits in its own cell; strict-> replacement in
    add() means an equal-fitness mutant cannot evict it."""
    cfg = DRQConfig(
        out_dir=str(tmp_path), token_budget=0, llm=LLMConfig(mock=True),
        me=MapElitesConfig(iterations=1, init_random=2, batch_size=2,
                           seed_with_champions=True),
    )
    drq = DRQ(_FixedDomain(), cfg)
    # a prior champion with a genome no init/mutant reproduces (distinct length)
    champ = Entity(genome="CHAMPION_SEED", fitness=1.0,
                   behavior=(13.0, 0.0), cell=(1, 0))
    drq.champions.append(champ)
    me = drq._solver_step()
    assert "CHAMPION_SEED" in {e.genome for e in me.elites()}


def test_seed_with_champions_disabled_does_not_reinsert(tmp_path):
    """Guard the flag's other branch: with seeding off, a prior champion must NOT
    be injected into a later round's archive."""
    cfg = DRQConfig(
        out_dir=str(tmp_path), token_budget=0, llm=LLMConfig(mock=True),
        me=MapElitesConfig(iterations=1, init_random=2, batch_size=2,
                           seed_with_champions=False),
    )
    drq = DRQ(_FixedDomain(), cfg)
    drq.champions.append(Entity(genome="CHAMPION_SEED", fitness=1.0,
                                behavior=(13.0, 0.0), cell=(1, 0)))
    me = drq._solver_step()
    assert "CHAMPION_SEED" not in {e.genome for e in me.elites()}


class _NullAdversaryDomain(_FixedDomain):
    """Co-evolutionary domain whose adversary never produces a usable challenge,
    forcing _adversary_step onto its seed-challenge fallback."""
    name = "null_adversary"

    def is_coevolutionary(self):
        return True

    def new_challenge(self, llm, target_genome):
        return None                                  # adversary always fails


def test_adversary_step_falls_back_to_seeds(tmp_path):
    """If the adversary yields nothing usable, the round's opponent must fall
    back to the domain's seed challenges rather than being empty."""
    cfg = DRQConfig(
        out_dir=str(tmp_path), token_budget=0, llm=LLMConfig(mock=True),
        me=MapElitesConfig(iterations=1, init_random=1, batch_size=1),
    )
    domain = _NullAdversaryDomain()
    drq = DRQ(domain, cfg)
    opp = drq._adversary_step(0, None)
    assert list(opp.challenges) == list(domain.seed_challenges)  # fell back
    assert len(opp.challenges) == 1


def test_budget_halts_run_before_all_rounds(tmp_path):
    """A cumulative token ceiling must halt the outer loop at a round boundary:
    fewer than the requested rounds are recorded and _halted is set."""
    # Text2SQLDomain actually calls the LLM (new_genome/mutate/fitness), so the
    # shared budget accrues; _FixedDomain never chats and would never halt.
    cfg = DRQConfig(
        rounds=5, out_dir=str(tmp_path), token_budget=1,   # 1 token = halt after round 0
        llm=LLMConfig(mock=True),
        me=MapElitesConfig(iterations=1, init_random=2, batch_size=2),
    )
    drq = DRQ(Text2SQLDomain(), cfg)
    drq.run()
    assert drq._halted is True
    rounds = [json.loads(line) for line in open(tmp_path / "run.jsonl")]
    assert 0 < len(rounds) < 5                        # stopped early, but ran ≥1
    assert drq.budget.snapshot()["tokens"] >= 1       # actually accrued spend


def test_evaluate_lineage_builds_full_curve(tmp_path):
    """evaluate_lineage must emit one curve point per champion, each carrying the
    round, its train_fitness, and the delegated generality/per_tag fields."""
    from drq.generality import evaluate_lineage

    champ_path = tmp_path / "champions.json"
    champ_path.write_text(json.dumps([
        {"round": 0, "fitness": 0.2, "genome": "g0", "cell": [0, 0]},
        {"round": 1, "fitness": 0.6, "genome": "g1", "cell": [1, 0]},
    ]))
    heldout = list(_FixedDomain().seed_challenges)
    worker = LLMClient(LLMConfig(mock=True))
    curve = evaluate_lineage(str(champ_path), heldout, worker, _FixedDomain())
    assert [p["round"] for p in curve] == [0, 1]
    assert [p["train_fitness"] for p in curve] == [0.2, 0.6]
    for p in curve:
        assert set(p) >= {"round", "train_fitness", "generality",
                          "n_heldout", "per_tag"}
        assert p["generality"] == 1.0                # _FixedDomain scores all right
