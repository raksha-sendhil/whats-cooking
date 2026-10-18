"""Retention: keep the database small enough to store and move between scheduled runs."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from whats_cooking.config import Settings
from whats_cooking.db import DB

log = logging.getLogger(__name__)

DEFAULTS = {"low_signal_days": 7, "items_days": 45, "snapshots_days": 30, "scores_days": 14,
            "scores_kept_per_run": 600, "embeddings_days": 16}


def run_prune(settings: Settings, db: DB, now: datetime | None = None, vacuum: bool = True) -> dict:
    cfg = {**DEFAULTS, **(settings.brief.get("retention") or {})}
    now = now or datetime.now(UTC)
    iso = lambda days: (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    c = db.conn
    counts = {}
    with c:
        featured = "SELECT topic_id FROM brief_topics"
        # 1. Low-signal: alone in their topic, never featured, older than a week.
        c.execute("CREATE TEMP TABLE IF NOT EXISTS doomed (id TEXT PRIMARY KEY)")
        c.execute("DELETE FROM doomed")
        c.execute(f"""
            INSERT OR IGNORE INTO doomed SELECT it.id FROM items it
            LEFT JOIN topic_items ti ON ti.item_id = it.id
            LEFT JOIN topics t ON t.id = ti.topic_id
            WHERE it.last_seen_at < ? AND (t.id IS NULL OR (t.item_count <= 1 AND t.id NOT IN ({featured})))
        """, (iso(cfg["low_signal_days"]),))
        # 2. Everything old, unless its topic made a brief.
        c.execute(f"""
            INSERT OR IGNORE INTO doomed SELECT it.id FROM items it
            LEFT JOIN topic_items ti ON ti.item_id = it.id
            WHERE it.last_seen_at < ? AND (ti.topic_id IS NULL OR ti.topic_id NOT IN ({featured}))
        """, (iso(cfg["items_days"]),))
        counts["items"] = c.execute("SELECT COUNT(*) FROM doomed").fetchone()[0]
        for table, col in (("item_refs", "item_id"), ("metric_snapshots", "item_id"), ("embeddings", "item_id"),
                           ("enrichments", "item_id"), ("topic_items", "item_id"), ("items", "id")):
            c.execute(f"DELETE FROM {table} WHERE {col} IN (SELECT id FROM doomed)")
        # Topics with no items left (keep merged stubs pointing at live topics).
        c.execute("""
            DELETE FROM topics WHERE merged_into IS NULL AND id NOT IN (SELECT DISTINCT topic_id FROM topic_items)
        """)
        c.execute("DELETE FROM topics WHERE merged_into IS NOT NULL AND merged_into NOT IN (SELECT id FROM topics)")
        # 3. Old history.
        counts["snapshots"] = c.execute("DELETE FROM metric_snapshots WHERE taken_at < ?", (iso(cfg["snapshots_days"]),)).rowcount
        counts["embeddings"] = c.execute("""
            DELETE FROM embeddings WHERE item_id IN (SELECT id FROM items WHERE first_seen_at < ?)
        """, (iso(cfg["embeddings_days"]),)).rowcount
        counts["scores"] = c.execute("DELETE FROM topic_scores WHERE scored_at < ?", (iso(cfg["scores_days"]),)).rowcount
        # Only keep the top-N scores of each scoring run (history for the 3/7/14-day re-scoring later).
        counts["scores"] += c.execute("""
            DELETE FROM topic_scores WHERE rowid IN (
                SELECT rowid FROM (
                    SELECT rowid, ROW_NUMBER() OVER (PARTITION BY scored_at ORDER BY score DESC) AS rn FROM topic_scores
                ) WHERE rn > ?)
        """, (cfg["scores_kept_per_run"],)).rowcount
        c.execute("DROP TABLE doomed")
        # Problems: keep 60 days of complaint evidence; analysed problems keep their cards and evidence.
        counts["mentions"] = c.execute("""
            DELETE FROM pain_mentions WHERE first_seen_at < ? AND mention_type = 'user'
              AND (problem_id IS NULL OR problem_id NOT IN (SELECT id FROM problems WHERE card IS NOT NULL))
        """, (iso(cfg.get("mentions_days", 60)),)).rowcount
        c.execute("""
            DELETE FROM pain_mentions WHERE mention_type = 'company' AND item_id NOT IN (SELECT id FROM items)
        """)
        c.execute("""
            DELETE FROM problems WHERE card IS NULL AND id NOT IN (SELECT DISTINCT problem_id FROM pain_mentions
                                                                  WHERE problem_id IS NOT NULL)
        """)
        c.execute("DELETE FROM mined WHERE mined_at < ?", (iso(7),))
        # Long game: keep judged progress for good; drop unjudged or unrelated matches after a while.
        c.execute("""DELETE FROM challenge_matches WHERE matched_at < ?
                     AND (verdict IS NULL OR verdict = 'not related')""", (iso(30),))
    if vacuum:
        c.execute("VACUUM")
    log.info("pruned %s", counts)
    return counts
