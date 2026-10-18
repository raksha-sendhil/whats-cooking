import json

import numpy as np

from whats_cooking.models import Item
from whats_cooking.pipeline.group import DEFAULTS, _Matcher, run_group, title_tokens


class FakeEmbedder:
    """Deterministic vectors: texts sharing a 'story' word get nearly the same vector."""

    name = DEFAULTS["model"]

    def __init__(self, stories: dict[str, int], dim: int = 16):
        self.stories, self.dim = stories, dim

    def embed(self, texts, batch_size=32):
        out = []
        for t in texts:
            v = np.full(self.dim, 0.01, dtype=np.float32)
            for word, axis in self.stories.items():
                if word in t.lower():
                    v[axis] += 1.0
            out.append(v / np.linalg.norm(v))
        return np.array(out)


def _add(db, run, **kw):
    db.upsert_items([Item(**kw)], run)


def _topics(db):
    rows = db.conn.execute(
        "SELECT ti.topic_id, it.id FROM topic_items ti JOIN items it ON it.id = ti.item_id ORDER BY it.id"
    ).fetchall()
    groups: dict[int, set[str]] = {}
    for tid, iid in rows:
        groups.setdefault(tid, set()).add(iid)
    return sorted(groups.values(), key=lambda s: sorted(s))


def test_same_story_across_sources_groups_and_different_story_doesnt(db, settings):
    run = db.start_run("collect")
    _add(db, run, source="hackernews", native_id="1", kind="story", title="EmbeddingGemma 2: open embedding model",
         url="https://blog.google/embeddinggemma")
    _add(db, run, source="rss", native_id="a", kind="article", title="Google releases EmbeddingGemma 2 embedding model",
         url="https://news.example/egemma")
    _add(db, run, source="lobsters", native_id="x", kind="story", title="Rust 2.0 compiler rewrite announced",
         url="https://rust.example/2")
    run_group(settings, db, embedder=FakeEmbedder({"embeddinggemma": 0, "rust": 1}))
    assert _topics(db) == [{"hackernews:1", "rss:a"}, {"lobsters:x"}]


def test_shared_ref_links_even_when_text_differs(db, settings):
    run = db.start_run("collect")
    _add(db, run, source="huggingface", native_id="paper:2610.00001", kind="paper", title="A long paper title",
         url="https://huggingface.co/papers/2610.00001")
    _add(db, run, source="arxiv", native_id="2610.00001", kind="paper", title="Totally different wording",
         url="https://arxiv.org/abs/2610.00001")
    run_group(settings, db, embedder=FakeEmbedder({"paper": 0, "wording": 1}))
    assert _topics(db) == [{"arxiv:2610.00001", "huggingface:paper:2610.00001"}]


def test_distinct_papers_on_same_theme_stay_separate(db, settings):
    run = db.start_run("collect")
    titles = ["Agent memory via retrieval gates", "Compressing agent memory traces", "Benchmarking agent memory depth"]
    for i, title in enumerate(titles):
        _add(db, run, source="arxiv", native_id=f"2610.0000{i}", kind="paper",
             title=title, url=f"https://arxiv.org/abs/2610.0000{i}")
    # Shared theme axis + a paper-specific axis: similar (cos ~0.5-0.9) but not the same paper.
    run_group(settings, db, embedder=FakeEmbedder({"agent memory": 0, "retrieval": 1, "compressing": 2, "benchmarking": 3}))
    assert len(_topics(db)) == 3


def test_baseline_items_are_not_grouped(db, settings):
    run = db.start_run("collect")
    _add(db, run, source="pages", native_id="u", kind="article", title="Old announcement from last year",
         url="https://lab.example/news/old", extra={"baseline": True})
    stats = run_group(settings, db, embedder=FakeEmbedder({}))
    assert stats.skipped_baseline == 1 and _topics(db) == []


def test_incremental_runs_join_existing_topics(db, settings):
    emb = FakeEmbedder({"mistral large 4": 0})
    run = db.start_run("collect")
    _add(db, run, source="hackernews", native_id="1", kind="story", title="Mistral Large 4 released", url="https://m.ai/l4")
    run_group(settings, db, embedder=emb)
    run2 = db.start_run("collect")
    _add(db, run2, source="reddit", native_id="t3_x", kind="story", title="Mistral Large 4 benchmarks look great",
         url="https://reddit.com/r/LocalLLaMA/x")
    run_group(settings, db, embedder=emb)
    assert _topics(db) == [{"hackernews:1", "reddit:t3_x"}]
    topic = db.conn.execute("SELECT source_count, sources FROM topics WHERE merged_into IS NULL").fetchone()
    assert topic["source_count"] == 2 and set(json.loads(topic["sources"])) == {"hackernews", "reddit"}


def test_bridge_item_merges_split_topics(db, settings):
    run = db.start_run("collect")
    _add(db, run, source="hackernews", native_id="1", kind="story", title="Beam model launch", url="https://a.example/beam")
    _add(db, run, source="rss", native_id="2", kind="article", title="Reflection's new open model", url="https://b.example/refl")
    run_group(settings, db, embedder=FakeEmbedder({"beam": 0, "reflection": 1}))
    assert len(_topics(db)) == 2
    run2 = db.start_run("collect")
    _add(db, run2, source="bluesky", native_id="p", kind="post", title="Reflection Beam is out",
         url="https://a.example/beam", text="https://b.example/refl")
    run_group(settings, db, embedder=FakeEmbedder({"beam": 0, "reflection": 1}))
    # The post shares the HN story's URL, so it must land in the HN story's topic.
    groups = _topics(db)
    assert any({"hackernews:1", "bluesky:p"} <= g for g in groups)


def test_release_names():
    from whats_cooking.pipeline.group import release_names
    assert release_names("Introducing Gemini 4 Argon, our best model") == {"gemini 4 argon"}
    assert release_names("Microsoft unveils MAI-Base-2") == {"mai base 2"}
    assert release_names("How fast is Python 3.15?") == {"python 3.15"}
    for t in ("Claude Code UI update", "Top 10 tools", "A Commodore 64 font", "Who is hiring? (October 2026)"):
        assert release_names(t) == set(), t


def test_new_release_name_merges_split_topics_but_old_names_dont(db, settings):
    from datetime import UTC, datetime, timedelta
    run = db.start_run("collect")
    now, old = datetime.now(UTC), datetime.now(UTC) - timedelta(days=9)
    db.upsert_items([
        Item("rss", "a", "article", "Introducing Gemini 4 Argon", url="https://g.example/a", created_at=now),
        Item("discourse", "b", "thread", "Gemini 4 Argon API keeps timing out", url="https://f.example/b", created_at=now),
        Item("hackernews", "c", "story", "I used Opus 5.5 to write a game", url="https://h.example/c", created_at=old),
        Item("reddit", "d", "story", "Opus 5.5 hit my usage limit", url="https://r.example/d", created_at=now),
    ], run)
    # Vectors far apart, so only the name could join them.
    run_group(settings, db, embedder=FakeEmbedder({"introducing": 0, "timing": 1, "game": 2, "limit": 3}))
    groups = _topics(db)
    assert {"rss:a", "discourse:b"} in groups                     # fresh release name: merged
    assert {"hackernews:c"} in groups and {"reddit:d"} in groups  # "Opus 5.5" first seen 9 days ago: not merged


def test_overlap_weights_rare_words():
    m = _Matcher(DEFAULTS["rules"], {"embeddinggemma": 5.0, "model": 0.5, "google": 1.0}, 3.0)
    a, b = title_tokens("google/embeddinggemma-2"), title_tokens("EmbeddingGemma 2: an open model")
    assert m.overlap(a, b) > 0.7
    assert m.overlap(title_tokens("a new model"), title_tokens("another model")) < m.overlap(a, b)


def test_artifacts_need_near_identity():
    m = _Matcher(DEFAULTS["rules"], {}, 3.0)
    t1, t2 = title_tokens("owner/vela-payments: payments"), title_tokens("owner/vela-server: backend")
    assert not m.matches(0.85, "repo", t1, "repo", t2)
    assert m.matches(0.85, "story", title_tokens("Vela payments launch"), "article", title_tokens("Vela payments launch"))
