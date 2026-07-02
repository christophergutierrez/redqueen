import pytest
from drq.domains.text2sql import (
    Challenge, ChallengeSet, exec_match, extract_sql, Text2SQLDomain,
)


SCHEMA = (
    "CREATE TABLE t(id INTEGER, val INTEGER);"
    "INSERT INTO t VALUES (1, 10), (2, 20), (3, 30);"
)


def test_exec_match_correct():
    gold = "SELECT SUM(val) FROM t;"
    pred = "SELECT SUM(val) FROM t;"
    assert exec_match(SCHEMA, gold, pred) is True


def test_exec_match_wrong_result():
    gold = "SELECT SUM(val) FROM t;"
    pred = "SELECT COUNT(*) FROM t;"
    assert exec_match(SCHEMA, gold, pred) is False


def test_exec_match_broken_pred():
    gold = "SELECT val FROM t WHERE id=1;"
    pred = "NOT VALID SQL!!!"
    assert exec_match(SCHEMA, gold, pred) is False


def test_exec_match_broken_gold():
    gold = "NOT VALID SQL!!!"
    pred = "SELECT val FROM t WHERE id=1;"
    assert exec_match(SCHEMA, gold, pred) is False


def test_exec_match_order_insensitive():
    gold = "SELECT val FROM t ORDER BY val ASC;"
    pred = "SELECT val FROM t ORDER BY val DESC;"
    # Both return same rows; sorted comparison should still match
    assert exec_match(SCHEMA, gold, pred) is True


def test_extract_sql_fenced():
    text = "Here is the answer:\n```sql\nSELECT 1;\n```"
    assert extract_sql(text).strip() == "SELECT 1;"


def test_extract_sql_fenced_no_lang():
    text = "```\nSELECT 2;\n```"
    assert "SELECT 2" in extract_sql(text)


def test_extract_sql_plain_select():
    text = "The answer is SELECT * FROM t WHERE id=1"
    assert extract_sql(text).startswith("SELECT")


def test_extract_sql_with_prefix():
    text = "Sure! Here you go:\nSELECT id FROM t;"
    result = extract_sql(text)
    assert "SELECT" in result


def test_extract_sql_plain_with():
    text = "WITH cte AS (SELECT 1 x) SELECT x FROM cte;"
    assert extract_sql(text).startswith("WITH")


def test_extract_sql_no_sql_returns_input():
    text = "There is no SQL here."
    assert extract_sql(text) == text.strip()


def test_challenge_to_dict_roundtrip():
    ch = Challenge(
        schema_sql="CREATE TABLE x(id INTEGER);",
        question="How many rows?",
        gold_sql="SELECT COUNT(*) FROM x;",
        tags=["agg"],
    )
    d = ch.to_dict()
    ch2 = Challenge(**d)
    assert ch2.schema_sql == ch.schema_sql
    assert ch2.question == ch.question
    assert ch2.gold_sql == ch.gold_sql
    assert ch2.tags == ch.tags


def test_challenge_set_to_dict():
    ch = Challenge("CREATE TABLE x(id INTEGER);", "?", "SELECT 1;", [])
    cs = ChallengeSet(round=3, challenges=[ch])
    d = cs.to_dict()
    assert d["round"] == 3
    assert len(d["challenges"]) == 1
    assert d["challenges"][0]["gold_sql"] == "SELECT 1;"


def test_wrap_opponent():
    domain = Text2SQLDomain()
    ch = Challenge("CREATE TABLE x(id INTEGER);", "?", "SELECT 1;", [])
    cs = domain.wrap_opponent(0, [ch])
    assert isinstance(cs, ChallengeSet)
    assert cs.round == 0
    assert cs.challenges[0] is ch
