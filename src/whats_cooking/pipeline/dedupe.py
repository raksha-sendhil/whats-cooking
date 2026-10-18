"""AI check for near-duplicate topics among the top candidates.

Grouping is tuned to avoid wrong merges, so a few same-story topics stay split
("Sharing AI progress in mathematics" vs "OpenAI publishes math manuscripts").
One cheap Haiku call reviews likely pairs among the top topics and merges confirmed ones.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import numpy as np

from whats_cooking.config import Settings
from whats_cooking.db import DB
from whats_cooking.llm import call_claude
from whats_cooking.pipeline.group import _Matcher, _idf, merge_topics, title_tokens
from whats_cooking.pipeline.group import DEFAULTS as GROUP_DEFAULTS
from whats_cooking.pipeline.score import TopicScore

log = logging.getLogger(__name__)

SYSTEM = (
    "You decide whether pairs of news topics are about the same underlying story, event, release or thing. "
    "Different stories that merely share a theme or company are NOT the same. Be strict."
)
SCHEMA = {
    "type": "object",
    "properties": {"same": {"type": "array", "items": {
        "type": "object", "properties": {"pair": {"type": "integer"}}, "required": ["pair"]}}},
    "required": ["same"],
}


def candidate_pairs(db: DB, scores: list[TopicScore], n: int, max_pairs: int, min_sim: float) -> list[tuple[int, int, float]]:
    top = [s.topic_id for s in scores[:n]]
    if len(top) < 2:
        return []
    rows = {r[0]: r for r in db.conn.execute(
        f"SELECT id, title, centroid FROM topics WHERE id IN ({','.join('?' * len(top))})", top)}
    top = [t for t in top if t in rows and rows[t][2]]
    vecs = np.stack([np.frombuffer(rows[t][2], dtype=np.float32) for t in top])
    sims = vecs @ vecs.T
    since = (datetime.now(UTC) - timedelta(days=GROUP_DEFAULTS["window_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    matcher = _Matcher(GROUP_DEFAULTS["rules"], *_idf(db.conn, since))
    toks = [title_tokens(rows[t][1]) for t in top]
    pairs = []
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            if sims[i, j] >= min_sim or matcher.overlap(toks[i], toks[j]) >= 0.35:
                pairs.append((top[i], top[j], float(sims[i, j])))
    pairs.sort(key=lambda p: -p[2])
    return pairs[:max_pairs]


def _describe(db: DB, tid: int) -> str:
    title = db.conn.execute("SELECT title FROM topics WHERE id=?", (tid,)).fetchone()[0]
    items = [r[0] for r in db.conn.execute(
        "SELECT it.title FROM topic_items ti JOIN items it ON it.id = ti.item_id WHERE ti.topic_id=? LIMIT 4", (tid,))]
    others = "; ".join(t[:110] for t in items if t != title)
    return f"{title[:150]}" + (f" (also: {others})" if others else "")


def run_dedupe(settings: Settings, db: DB, scores: list[TopicScore], runner=None) -> int:
    """Returns the number of merges performed."""
    cfg = {"enabled": True, "candidates": 60, "max_pairs": 40, "min_similarity": 0.40, **(settings.brief.get("dedupe") or {})}
    if not cfg["enabled"]:
        return 0
    pairs = candidate_pairs(db, scores, cfg["candidates"], cfg["max_pairs"], cfg["min_similarity"])
    if not pairs:
        return 0
    lines = [f"Pair {k}:\n  A: {_describe(db, a)}\n  B: {_describe(db, b)}" for k, (a, b, _) in enumerate(pairs)]
    prompt = ("For each pair, decide if A and B are the same story. Return only the pair numbers that are the same.\n\n"
              + "\n".join(lines))
    r = call_claude(settings, db, "dedupe", prompt, SYSTEM, schema=SCHEMA, runner=runner)
    if not r.ok or not r.data:
        return 0
    score_of = {s.topic_id: s.score for s in scores}
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    merges = 0
    for entry in r.data.get("same", []):
        k = entry.get("pair")
        if not isinstance(k, int) or not 0 <= k < len(pairs):
            continue
        a, b = find(pairs[k][0]), find(pairs[k][1])
        if a == b:
            continue
        keep, drop = (a, b) if score_of.get(a, 0) >= score_of.get(b, 0) else (b, a)
        merge_topics(db.conn, drop, keep)
        parent[drop] = keep
        merges += 1
        log.info("merged topic %d into %d", drop, keep)
    return merges
