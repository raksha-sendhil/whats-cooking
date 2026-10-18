import json
from datetime import date

import numpy as np

from whats_cooking.db import now_iso
from whats_cooking.models import Item
from whats_cooking.pipeline.group import run_group
from whats_cooking.pipeline.longgame import (BASELINE, candidates_to_judge, judge, keyword_pattern, longgame_view,
                                             match_topics, months_back, refresh_trends, trend_series)
from whats_cooking.pipeline.score import run_score
from test_group import FakeEmbedder
from test_llm import _ok_output

CHALLENGES = [
    {"id": "no-gpu", "focus": True, "title": "Running AI without a GPU", "summary": "cpu", "progress": "quantization",
     "keywords": ["quantiz", "llama.cpp"], "history": ["llama.cpp"]},
    {"id": "voice", "title": "Real-time voice", "summary": "voice", "progress": "latency",
     "keywords": ["voice agent"], "history": ["voice AI"]},
]


def test_keyword_pattern_matches_word_starts_only():
    p = keyword_pattern(["quantiz", "llama.cpp", "rag"])
    assert p.search("New 4-bit Quantization trick") and p.search("llama.cpp adds CPU kernels")
    assert not p.search("storage drag race")          # 'rag' must start a word


def test_months_back():
    assert months_back(2, date(2026, 1, 15)) == ["2025-11", "2025-12", "2026-01"]


def test_trend_share_is_per_10k_hn_stories(db):
    with db.conn:
        db.conn.executemany("INSERT INTO challenge_trends VALUES (?,?,?,?,?)", [
            (BASELINE, "", "2026-09", 20000, now_iso()), ("no-gpu", "llama.cpp", "2026-09", 30, now_iso()),
            ("no-gpu", "quantization", "2026-09", 10, now_iso())])
    s = trend_series(db, "no-gpu", ["2026-08", "2026-09"])
    assert s[0]["share"] is None and s[1] == {"period": "2026-09", "count": 40, "share": 20.0}


def test_refresh_counts_in_week_windows_and_skips_known_months(db, settings):
    settings.challenges = CHALLENGES[:1]
    settings.brief = {"longgame": {"trend_months": 1}}

    class F:
        calls = 0
        def get_json(self, url, params=None):
            F.calls += 1
            return {"nbHits": 3}
    n = refresh_trends(settings, db, fetcher=F(), budget_s=60)
    assert n == 4                                     # (baseline + 1 phrase) x (last month + this month)
    assert F.calls >= 4 * 4                           # several 7-day windows per month
    calls = F.calls
    refresh_trends(settings, db, fetcher=F(), budget_s=60)
    assert F.calls - calls < calls                    # past months aren't fetched again


def _seed(db, settings):
    settings.challenges = CHALLENGES
    run = db.start_run("collect")
    db.upsert_items([
        Item("hackernews", "1", "story", "llama.cpp gets 2x faster quantization on CPUs", url="https://x.example/q",
             discussion_url="https://news.ycombinator.com/item?id=1", metrics={"points": 300, "comments": 90}),
        Item("rss", "a", "article", "Faster llama.cpp quantization lands", url="https://y.example/q",
             extra={"feed_group": "expert"}),
        Item("hackernews", "2", "story", "A new voice agent framework", url="https://z.example/v", metrics={"points": 50}),
    ], run)
    emb = FakeEmbedder({"llama.cpp": 0, "quantiz": 0, "cpu": 0, "voice": 1})
    run_group(settings, db, embedder=emb)
    return run_score(settings, db, embedder=FakeEmbedder({})), emb


def test_match_needs_keyword_and_meaning(db, settings):
    scores, emb = _seed(db, settings)
    assert match_topics(settings, db, scores, embedder=emb) == 2
    rows = dict(db.conn.execute("SELECT challenge_id, keyword FROM challenge_matches").fetchall())
    assert rows == {"no-gpu": "llama.cpp", "voice": "voice agent"}


def test_focus_challenges_are_judged_first_and_progress_is_shown(db, settings):
    scores, emb = _seed(db, settings)
    match_topics(settings, db, scores, embedder=emb)
    settings.brief = {"longgame": {"max_judge": 1}}
    cands = candidates_to_judge(settings, db)
    assert [c["id"] for c, _ in cands] == ["no-gpu"]             # the cap keeps the focus challenge
    verdict = {"candidate": "C0", "verdict": "real progress", "headline": "llama.cpp runs 2x faster on CPUs",
               "what_changed": "w", "why_it_helps": "h", "for_you": "f"}
    progress, r = judge(settings, db, runner=lambda a, p, t: _ok_output({"verdicts": [verdict]}))
    assert r.ok and progress == [("no-gpu", cands[0][1]["topic_id"])]
    view = {c["id"]: c for c in longgame_view(settings, db)}
    assert view["no-gpu"]["entries"][0]["headline"] == "llama.cpp runs 2x faster on CPUs"
    assert view["no-gpu"]["focus"] and not view["voice"]["entries"]
