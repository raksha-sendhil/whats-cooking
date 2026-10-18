"""Scoring, brief selection/rendering, watch lists and pruning on a small synthetic database."""

import json
from datetime import UTC, datetime, timedelta

import numpy as np

from whats_cooking.db import now_iso
from whats_cooking.models import Item
from whats_cooking.pipeline.brief import run_brief, select_topics
from whats_cooking.pipeline.group import run_group
from whats_cooking.pipeline.prune import run_prune
from whats_cooking.pipeline.score import engagement_value, latest_scores, run_score, topic_type
from whats_cooking.pipeline.watch import update_bluesky_watch, update_repo_watch
from test_group import FakeEmbedder
from test_llm import _ok_output

NOW = datetime.now(UTC)


def _seed(db, settings):
    run = db.start_run("collect")
    items = [
        Item("hackernews", "1", "story", "Acme releases Widget model", url="https://acme.example/widget",
             discussion_url="https://news.ycombinator.com/item?id=1",
             created_at=NOW, metrics={"points": 500, "comments": 200}),
        Item("rss", "w", "article", "Acme Widget model is out", url="https://news.example/widget",
             created_at=NOW, extra={"feed_group": "expert"}),
        Item("github", "acme/widget", "repo", "acme/widget: the Widget model", url="https://github.com/acme/widget",
             created_at=NOW, metrics={"stars": 900, "stars_today": 400}),
        Item("hackernews", "2", "story", "Some quiet blog post", url="https://blog.example/quiet",
             created_at=NOW - timedelta(hours=60), metrics={"points": 31, "comments": 2}),
    ] + [Item("hackernews", f"x{i}", "story", f"Filler story number {i}", url=f"https://f.example/{i}",
              created_at=NOW, metrics={"points": 40 + i}) for i in range(5)]
    db.upsert_items(items, run)
    # The HN story and the repo link via the README-style text ref; the RSS article via similarity.
    run_group(settings, db, embedder=FakeEmbedder({"widget": 0, "quiet": 1, "filler": 2}))


def test_engagement_values_and_types():
    assert engagement_value("hackernews", "story", {"points": 100, "comments": 50}, None) == 125
    assert engagement_value("github", "repo", {"stars": 70}, 7) == 10
    assert engagement_value("arxiv", "paper", {}, None) is None
    assert topic_type({"story"}, ["Acme raises $20M Series A"]) == "funding"
    assert topic_type({"ask", "thread"}, ["Why is X so slow?"]) == "pain"


def test_multi_source_topic_outranks_single_and_old(db, settings):
    _seed(db, settings)
    scores = run_score(settings, db, embedder=FakeEmbedder({}))
    titles = dict(db.conn.execute("SELECT id, title FROM topics"))
    best = scores[0]
    assert "Widget" in titles[best.topic_id]
    assert best.features["spread"] > 0 and best.features["curation"] > 0
    quiet = next(s for s in scores if "quiet" in titles[s.topic_id])
    assert quiet.features["freshness"] < 0.4 and quiet.score < best.score


def test_official_lab_release_gets_boost(db, settings):
    run = db.start_run("collect")
    db.upsert_items([
        Item("rss", "lab", "article", "Introducing Gemini 4 Argon", url="https://deepmind.example/argon",
             channel="deepmind", created_at=NOW, extra={"feed_group": "lab"}),
        Item("rss", "blog", "article", "My notes on Rust lifetimes", url="https://blog.example/rust",
             channel="some-blog", created_at=NOW, extra={"feed_group": "lab"}),
    ], run)
    run_group(settings, db, embedder=FakeEmbedder({"argon": 0, "rust": 1}))
    settings.scoring = {"official_sources": ["deepmind"]}
    scores = {db.conn.execute("SELECT title FROM topics WHERE id=?", (s.topic_id,)).fetchone()[0]: s
              for s in run_score(settings, db, embedder=FakeEmbedder({}))}
    assert scores["Introducing Gemini 4 Argon"].features["official"] == 1.0
    assert scores["My notes on Rust lifetimes"].features["official"] == 0.0
    assert scores["Introducing Gemini 4 Argon"].score > scores["My notes on Rust lifetimes"].score


def test_topic_cap_and_repeat_suppression(db, settings):
    _seed(db, settings)
    run_score(settings, db, embedder=FakeEmbedder({}))
    cfg = {"max_topics": 2, "repeat_cooldown_days": 3, "repeat_if_new_items": 3}
    picked = select_topics(db, cfg, "2026-10-08")
    assert len(picked) == 2                                    # hard cap
    with db.conn:                                              # featured yesterday, no growth since
        db.conn.execute("INSERT INTO brief_topics VALUES ('2026-10-07', ?, 1.0, ?, 'top')",
                        (picked[0].topic_id, picked[0].features["items"]))
    again = select_topics(db, cfg, "2026-10-08")
    assert picked[0].topic_id not in {s.topic_id for s in again}


def test_brief_done_and_rendered(db, settings):
    _seed(db, settings)
    run_score(settings, db, embedder=FakeEmbedder({}))
    top = latest_scores(db)[0].topic_id
    data = {
        "themes": ["Widgets everywhere"],
        "top_stories": [{"topic": f"T{top}", "headline": "Widget lands", "what": "w", "why_it_matters": "m",
                         "so_what": "s", "skeptic": "k", "signal": "strong"}],
        "quick_hits": [], "study": [{"concept": "c", "why": "y", "start_with": "z"}],
    }
    r = run_brief(settings, db, runner=lambda a, p, t: _ok_output(data), now=NOW)
    md = r.path.read_text(encoding="utf-8")
    assert r.status == "done" and "### 1. Widget lands" in md and "news.ycombinator.com" in md
    assert db.conn.execute("SELECT section FROM brief_topics WHERE topic_id=?", (top,)).fetchone()[0] == "top"
    view = json.loads(db.conn.execute("SELECT data FROM briefs").fetchone()[0])   # what the reader site shows
    story = view["top_stories"][0]
    assert story["headline"] == "Widget lands" and story["sources"] and view["number"] == 1 and not view["fallback"]
    hn = next(s for s in story["sources"] if "news.ycombinator.com" in s["url"])
    assert hn["original"] == "https://acme.example/widget"     # Claude gets the thread AND the article
    assert run_brief(settings, db, runner=None, now=NOW).status == "exists"   # once per day


def test_no_claude_steps_when_todays_edition_exists(db, settings):
    from whats_cooking.pipeline.brief import local_day
    from whats_cooking.pipeline.daily import brief_due
    assert brief_due(db, settings, "always")
    with db.conn:
        db.conn.execute("INSERT INTO briefs (day, status, created_at) VALUES (?, 'done', 'x')", (local_day(settings),))
    assert not brief_due(db, settings, "always")               # already written: no usage spent
    assert brief_due(db, settings, "always", force=True)       # "Rewrite" ticked
    with db.conn:
        db.conn.execute("UPDATE briefs SET status='pending'")
    assert brief_due(db, settings, "always")                   # a failed one is retried


def test_brief_falls_back_and_retries_when_limited(db, settings):
    _seed(db, settings)
    run_score(settings, db, embedder=FakeEmbedder({}))
    limited = lambda a, p, t: (1, json.dumps({"is_error": True, "result": "usage limit reached"}), "")
    r = run_brief(settings, db, runner=limited, now=NOW)
    assert r.status == "pending" and "Signals-only" in r.path.read_text(encoding="utf-8")
    data = {"themes": [], "top_stories": [], "quick_hits": [], "study": []}
    assert run_brief(settings, db, runner=lambda a, p, t: _ok_output(data), now=NOW).status == "done"


def test_repo_watchlist_grows_and_expires(db, settings):
    _seed(db, settings)
    scores = run_score(settings, db, embedder=FakeEmbedder({}))
    settings.brief = {"watchlists": {"repos": {"min_score": 0.0}}}
    ch = update_repo_watch(settings, db, scores)
    assert "acme/widget" in ch.added and db.watchlist("github_repo") == ["acme/widget"]
    later = update_repo_watch(settings, db, [], now=NOW + timedelta(days=30))
    assert later.dropped == ["acme/widget"] and db.watchlist("github_repo") == []


def test_bluesky_follows_accounts_amplified_by_two_trackers(db, settings):
    settings.sources = {"bluesky": {"accounts": ["a.bsky.social", "b.bsky.social"]}}
    run = db.start_run("collect")
    db.upsert_items([
        Item("bluesky", "p1", "post", "x", author="new.bsky.social", channel="repost:a.bsky.social"),
        Item("bluesky", "p2", "post", "y", author="b.bsky.social", channel="b.bsky.social",
             extra={"quoted_author": "new.bsky.social"}),
        Item("bluesky", "p3", "post", "z", author="solo.bsky.social", channel="repost:a.bsky.social"),
    ], run)
    ch = update_bluesky_watch(settings, db)
    assert ch.added == ["new.bsky.social"]


def test_prune_drops_old_low_signal_but_keeps_featured(db, settings):
    _seed(db, settings)
    old = (NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.conn:
        db.conn.execute("UPDATE items SET last_seen_at=?, first_seen_at=?", (old, old))
        widget_topic = db.conn.execute(
            "SELECT topic_id FROM topic_items WHERE item_id='hackernews:1'").fetchone()[0]
        db.conn.execute("INSERT INTO brief_topics VALUES ('2026-10-01', ?, 1.0, 3, 'top')", (widget_topic,))
    run_prune(settings, db, vacuum=False)
    left = {r[0] for r in db.conn.execute("SELECT id FROM items")}
    assert "hackernews:1" in left and "hackernews:2" not in left
    assert db.conn.execute("SELECT COUNT(*) FROM topic_items WHERE item_id NOT IN (SELECT id FROM items)").fetchone()[0] == 0
