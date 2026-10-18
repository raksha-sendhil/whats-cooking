"""Fetch extra context for shortlisted topics only: top comments and README excerpts.

Comments are where the critique, use cases and complaints are; this is what lets the brief
say why something matters (or doesn't), not just what it is.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from whats_cooking.collectors.base import strip_html, truncate
from whats_cooking.db import DB, now_iso
from whats_cooking.http import Fetcher

log = logging.getLogger(__name__)

MAX_COMMENTS = 5
COMMENT_CHARS = 350
README_CHARS = 1200


def enrich_topics(db: DB, topic_ids: list[int], fetcher: Fetcher | None = None, max_items_per_topic: int = 3) -> int:
    """Enrich the most important items of each topic (cached). Returns the number of items fetched."""
    todo: list[tuple[str, str, str, str | None]] = []
    for tid in topic_ids:
        rows = db.conn.execute(
            """
            SELECT it.id, it.source, it.kind, it.native_id, it.url FROM topic_items ti
            JOIN items it ON it.id = ti.item_id
            LEFT JOIN enrichments en ON en.item_id = it.id
            WHERE ti.topic_id = ? AND en.item_id IS NULL
              AND (it.source IN ('hackernews', 'lobsters') OR (it.source = 'github' AND it.kind = 'repo'))
            ORDER BY CAST(COALESCE(json_extract(it.metrics, '$.comments'), 0) AS INTEGER) DESC
            LIMIT ?
            """,
            (tid, max_items_per_topic),
        ).fetchall()
        todo += [(r["id"], r["source"], r["native_id"], r["url"]) for r in rows]
    if not todo:
        return 0
    own = fetcher is None
    fetcher = fetcher or Fetcher()
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda t: _fetch_one(fetcher, *t), todo))
    finally:
        if own:
            fetcher.close()
    with db.conn:
        for (item_id, *_), (comments, readme) in zip(todo, results):
            db.conn.execute(
                "INSERT OR REPLACE INTO enrichments (item_id, comments, readme, fetched_at) VALUES (?,?,?,?)",
                (item_id, json.dumps(comments), readme, now_iso()),
            )
    return len(todo)


def _fetch_one(fetcher: Fetcher, item_id: str, source: str, native_id: str, url: str | None) -> tuple[list[str], str]:
    try:
        if source == "hackernews":
            return _hn_comments(fetcher, native_id), ""
        if source == "lobsters":
            return _lobsters_comments(fetcher, native_id), ""
        if source == "github":
            return [], _readme(fetcher, native_id)
    except Exception as exc:
        log.debug("enrich %s failed: %s", item_id, exc)
    return [], ""


def _hn_comments(fetcher: Fetcher, story_id: str) -> list[str]:
    # HN's API returns top-level comments in ranked order; the first few are the community's best.
    data = fetcher.get_json(f"https://hn.algolia.com/api/v1/items/{story_id}")
    out = []
    for c in data.get("children", []):
        text = strip_html(c.get("text"))
        if text and len(text) > 40:
            out.append(truncate(text, COMMENT_CHARS))
        if len(out) >= MAX_COMMENTS:
            break
    return out


def _lobsters_comments(fetcher: Fetcher, short_id: str) -> list[str]:
    data = fetcher.get_json(f"https://lobste.rs/s/{short_id}.json")
    top = sorted((c for c in data.get("comments", []) if c.get("depth", 0) == 0),
                 key=lambda c: -(c.get("score") or 0))
    return [truncate(strip_html(c.get("comment")), COMMENT_CHARS) for c in top[:MAX_COMMENTS] if c.get("comment")]


def _readme(fetcher: Fetcher, full_name: str) -> str:
    data = fetcher.get_json(f"https://api.github.com/repos/{full_name}/readme")
    raw = base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
    raw = re.sub(r"<[^>]+>|!\[[^\]]*\]\([^)]*\)|\[!\[.*?\]\(.*?\)\]\(.*?\)", " ", raw)  # html, images, badges
    raw = re.sub(r"\n\s*\n+", "\n", raw)
    return truncate(raw.strip(), README_CHARS)
