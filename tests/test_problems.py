import json

import pytest

from whats_cooking.db import now_iso
from whats_cooking.llm import check_auth, child_env
from whats_cooking.models import Item
from whats_cooking.pipeline.pains import _mine_thread, complaint_sentences, harvest
from whats_cooking.pipeline.problems import assign_problems, cards_due, render_board, score_problems, write_cards
from test_group import FakeEmbedder
from test_llm import _ok_output


def test_complaint_sentences_find_needs_not_chatter():
    text = ("Nice launch! I wish there was a tool that syncs my notes offline. "
            "> I wish there was a quoted reply\n"
            "The weather was great today. We spent hours every week reconciling invoices by hand.")
    found = [s for s, _, _ in complaint_sentences(text)]
    assert any("syncs my notes" in s for s in found)
    assert any("reconciling invoices" in s for s in found)
    assert not any("weather" in s or "quoted reply" in s for s in found)


def test_workarounds_outweigh_wishes():
    from whats_cooking.pipeline.pains import WORKAROUND
    workaround = complaint_sentences("We keep a spreadsheet to reconcile invoices every week because nothing exports cleanly.")
    wish = complaint_sentences("Someone should build a better invoice tool.")
    assert workaround and WORKAROUND.search(workaround[0][0])
    assert not wish                                   # a bare "someone should build X" is too weak on its own


def test_quoted_sentence_counts_once_per_thread():
    class F:
        def get_json(self, url):
            kids = [{"author": a, "text": "There's no way to export my data to another app."} for a in "abc"]
            return {"children": kids}
    out = _mine_thread(F(), {"id": "hackernews:1", "source": "hackernews", "native_id": "1",
                             "discussion_url": "u", "extra": "{}"})
    assert len(out) == 1


def _mention_items(db):
    run = db.start_run("collect")
    items = [Item("hackernews", f"s{i}", "ask", f"Ask HN thread {i}", text=t, author=a,
                  metrics={"points": 20}, url=f"https://news.ycombinator.com/item?id=s{i}")
             for i, (t, a) in enumerate([
                 ("There's no way to export invoices from Stripe to my accounting app.", "ann"),
                 ("Is there a tool to export invoices to accounting software automatically?", "bob"),
                 ("I wish there was a simple way to export invoices into accounting.", "cat"),
                 ("Why is there no decent way to self-host a password manager for families?", "dan"),
             ])]
    items.append(Item("yc_rfs", "ai-compliance", "request", "AI-Native Compliance Infrastructure",
                      text="AI-Native Compliance Infrastructure By X. Companies drown in compliance work...", channel="rfs"))
    db.upsert_items(items, run)


def test_problems_group_count_people_and_gate_on_evidence(db, settings):
    _mention_items(db)
    harvest(settings, db)
    emb = FakeEmbedder({"invoice": 0, "password": 1, "compliance": 2})
    stats = assign_problems(settings, db, embedder=emb)
    assert stats["new_problems"] == 3            # invoices, passwords, the YC request
    score_problems(settings, db)
    rows = {r["kind"] + ":" + str(r["authors"]): r for r in db.conn.execute("SELECT * FROM problems")}
    invoices = next(r for r in db.conn.execute("SELECT * FROM problems WHERE kind='user' ORDER BY authors DESC"))
    assert invoices["authors"] == 3 and invoices["threads"] == 3
    due = cards_due(settings, db)
    assert invoices["id"] in due                  # 3 people, 3 threads: enough evidence
    lone = db.conn.execute("SELECT id FROM problems WHERE kind='user' AND authors=1").fetchone()[0]
    assert lone not in due                        # one person: not yet


def test_cards_written_and_non_problems_dismissed(db, settings):
    _mention_items(db)
    harvest(settings, db)
    assign_problems(settings, db, embedder=FakeEmbedder({"invoice": 0, "password": 1, "compliance": 2}))
    score_problems(settings, db)
    due = cards_due(settings, db)
    sol = [{"idea": "i", "how": "h", "why_not_done_yet": "w", "difficulty": "months"}]
    card = lambda pid, real: {"problem": f"P{pid}", "is_real": real, "title": f"T{pid}", "summary": "s", "evidence": "e",
                              "current_options": "c", "why_unsolved": "u", "solutions": sol, "opportunity": "o",
                              "first_step": "f"}
    data = {"cards": [card(due[0], True)] + [card(p, False) for p in due[1:]]}
    done, r = write_cards(settings, db, runner=lambda a, p, t: _ok_output(data))
    assert r.ok and done == [due[0]]
    statuses = dict(db.conn.execute("SELECT id, status FROM problems WHERE card IS NOT NULL"))
    assert statuses[due[0]] == "open" and all(statuses[p] == "dismissed" for p in due[1:])
    assert cards_due(settings, db) == []          # no re-analysis until evidence doubles
    board = render_board(settings, db)
    assert f"T{due[0]}" in board and "Why not done yet" in board


def test_child_env_strips_billing_vars(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    env = child_env("subscription")
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_USE_BEDROCK" not in env
    assert child_env("api_key")["ANTHROPIC_API_KEY"] == "sk-test"


@pytest.mark.parametrize("status, refused", [
    ({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"}, False),
    ({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "apiKeySource": "apiKeyHelper"}, True),
    ({"loggedIn": True, "apiProvider": "bedrock"}, True),
    ({"loggedIn": False}, True),
])
def test_check_auth_refuses_anything_but_a_subscription_login(monkeypatch, status, refused):
    import subprocess

    class P:
        stdout = json.dumps(status)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: P())
    assert (check_auth("claude", "subscription", {}) is not None) == refused
