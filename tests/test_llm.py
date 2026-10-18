import json

from whats_cooking.db import now_iso
from whats_cooking.llm import BudgetGuard, call_claude, parse_cli_output


def _ok_output(structured, cost=0.10, models=("claude-sonnet-5-5",)):
    return (0, json.dumps({
        "type": "result", "subtype": "success", "is_error": False, "result": json.dumps(structured),
        "total_cost_usd": cost, "duration_ms": 1234, "structured_output": structured,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "modelUsage": {m: {"inputTokens": 100, "outputTokens": 50 if "sonnet" in m else 5} for m in models},
    }), "")


def _spend(db, usd, purpose="brief"):
    with db.conn:
        db.conn.execute("INSERT INTO llm_calls (ts, purpose, model, status, cost_usd) VALUES (?,?,?,?,?)",
                        (now_iso(), purpose, "x", "ok", usd))


def test_parse_success_attributes_requested_model(db):
    r, usage, ms = parse_cli_output(_ok_output({"a": 1}, models=("claude-haiku-4-5", "claude-sonnet-5-5")), "sonnet")
    assert r.ok and r.data == {"a": 1} and r.cost_usd == 0.10 and ms == 1234
    assert r.model == "claude-sonnet-5-5"
    assert r.input_tokens == 200 and r.output_tokens == 55


def test_parse_usage_limit_is_limited_not_error():
    out = (1, json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "result": "Claude usage limit reached. Your limit will reset at 5pm"}), "")
    r, *_ = parse_cli_output(out, "sonnet")
    assert r.status == "limited"
    r, *_ = parse_cli_output((1, "", "something exploded"), "sonnet")
    assert r.status == "error" and "exploded" in r.error


def test_guard_normal_plan(db, settings):
    model, cap, notes = BudgetGuard(settings, db).plan("brief")
    assert model == "sonnet" and cap == 0.60 and notes == []


def test_guard_downgrades_then_skips_but_reserves_weekly(db, settings):
    settings.budget = {"daily_cap_usd": 10, "weekly_cap_usd": 5.0}
    _spend(db, 3.6)                                   # 72% of weekly
    model, _, notes = BudgetGuard(settings, db).plan("brief")
    assert model == "haiku" and "instead of sonnet" in notes[0]
    _spend(db, 1.0)                                   # 92%
    assert BudgetGuard(settings, db).plan("brief")[0] is None
    model, cap, _ = BudgetGuard(settings, db).plan("weekly")
    assert model == "sonnet" and abs(cap - 0.4) < 1e-9  # downgraded from opus, capped at what's left


def test_guard_daily_cap(db, settings):
    settings.budget = {"daily_cap_usd": 0.5}
    _spend(db, 0.5)
    model, _, notes = BudgetGuard(settings, db).plan("brief")
    assert model is None and "daily cap" in notes[0]


def test_call_claude_records_ledger_and_passes_cap(db, settings):
    seen = {}

    def runner(args, prompt, timeout):
        seen["args"], seen["prompt"] = args, prompt
        return _ok_output({"x": 1}, cost=0.12)

    r = call_claude(settings, db, "brief", "hello", "sys", schema={"type": "object"}, runner=runner)
    assert r.ok and seen["prompt"] == "hello"
    args = seen["args"]
    assert args[args.index("--max-budget-usd") + 1] == "0.60"
    assert args[args.index("--tools") + 1] == ""          # no tools for the brief
    row = db.conn.execute("SELECT purpose, status, cost_usd FROM llm_calls").fetchone()
    assert tuple(row) == ("brief", "ok", 0.12)


def test_skipped_call_never_runs(db, settings):
    settings.budget = {"weekly_cap_usd": 1.0}
    _spend(db, 0.95)

    def runner(*a):
        raise AssertionError("must not call Claude")

    r = call_claude(settings, db, "brief", "p", "s", runner=runner)
    assert r.status == "skipped"
