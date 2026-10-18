"""Watch lists that grow and shrink on their own.

  github_repo      repos appearing in top topics get their releases tracked; dropped when quiet.
  bluesky_account  accounts repeatedly amplified (reposted/quoted) by tracked experts get followed;
                   dropped when they stop showing up.
Static lists in config/sources.yaml are never touched.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from whats_cooking.config import Settings
from whats_cooking.db import DB, now_iso
from whats_cooking.pipeline.score import TopicScore

log = logging.getLogger(__name__)


@dataclass
class WatchChanges:
    added: list[str]
    dropped: list[str]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _upsert(db: DB, kind: str, value: str, reason: str, ts: str) -> bool:
    """Insert or refresh a watch entry. Returns True if it became active now."""
    row = db.conn.execute("SELECT active FROM watchlist WHERE kind=? AND value=?", (kind, value)).fetchone()
    db.conn.execute(
        """
        INSERT INTO watchlist (kind, value, added_at, last_hit_at, reason, active) VALUES (?,?,?,?,?,1)
        ON CONFLICT(kind, value) DO UPDATE SET last_hit_at=excluded.last_hit_at, reason=excluded.reason, active=1
        """,
        (kind, value, ts, ts, reason[:200]),
    )
    return row is None or not row[0]


def _expire(db: DB, kind: str, ttl_days: int, max_active: int, now: datetime) -> list[str]:
    cutoff = _iso(now - timedelta(days=ttl_days))
    dropped = [r[0] for r in db.conn.execute(
        "SELECT value FROM watchlist WHERE kind=? AND active=1 AND last_hit_at < ?", (kind, cutoff))]
    over = db.conn.execute(
        "SELECT value FROM watchlist WHERE kind=? AND active=1 AND last_hit_at >= ? ORDER BY last_hit_at DESC LIMIT -1 OFFSET ?",
        (kind, cutoff, max_active)).fetchall()
    dropped += [r[0] for r in over]
    db.conn.executemany("UPDATE watchlist SET active=0 WHERE kind=? AND value=?", [(kind, v) for v in dropped])
    return dropped


def update_repo_watch(settings: Settings, db: DB, scores: list[TopicScore], now: datetime | None = None) -> WatchChanges:
    cfg = {"from_top_topics": 30, "min_score": 0.25, "ttl_days": 21, "max_active": 40,
           **((settings.brief.get("watchlists") or {}).get("repos") or {})}
    now = now or datetime.now(UTC)
    static = {r.lower() for r in (settings.source_cfg("github").get("releases") or {}).get("repos", [])}
    ts, added = _iso(now), []
    with db.conn:
        for s in scores[: cfg["from_top_topics"]]:
            if s.score < cfg["min_score"]:
                break
            title = db.conn.execute("SELECT title FROM topics WHERE id=?", (s.topic_id,)).fetchone()[0]
            refs = db.conn.execute(
                """
                SELECT DISTINCT ir.ref FROM item_refs ir JOIN topic_items ti ON ti.item_id = ir.item_id
                WHERE ti.topic_id = ? AND ir.primary_ref = 1 AND ir.ref LIKE 'github:%'
                """,
                (s.topic_id,),
            )
            for (ref,) in refs:
                repo = ref.removeprefix("github:")
                if "#" in repo or "@" in repo or repo in static:
                    continue
                if _upsert(db, "github_repo", repo, f"in top topic: {title}", ts):
                    added.append(repo)
        dropped = _expire(db, "github_repo", cfg["ttl_days"], cfg["max_active"], now)
    return WatchChanges(added, dropped)


def update_bluesky_watch(settings: Settings, db: DB, now: datetime | None = None) -> WatchChanges:
    cfg = {"min_trackers": 2, "window_days": 14, "ttl_days": 30, "max_active": 30,
           **((settings.brief.get("watchlists") or {}).get("bluesky") or {})}
    now = now or datetime.now(UTC)
    static = set(settings.source_cfg("bluesky").get("accounts", []))
    following = static | set(db.watchlist("bluesky_account"))
    since = _iso(now - timedelta(days=cfg["window_days"]))
    trackers: dict[str, set[str]] = defaultdict(set)
    seen_own_posts: set[str] = set()
    for author, channel, extra in db.conn.execute(
        "SELECT author, channel, extra FROM items WHERE source='bluesky' AND first_seen_at >= ?", (since,)
    ):
        extra = json.loads(extra or "{}")
        if channel.startswith("repost:") and author:
            trackers[author].add(channel.removeprefix("repost:"))
        elif author:
            seen_own_posts.add(author)
            if (quoted := extra.get("quoted_author")) and author in following:
                trackers[quoted].add(author)
    ts, added = _iso(now), []
    with db.conn:
        for handle, who in trackers.items():
            if handle in static:
                continue
            if len(who) >= cfg["min_trackers"]:
                if _upsert(db, "bluesky_account", handle, f"amplified by {', '.join(sorted(who))[:150]}", ts):
                    added.append(handle)
        # A followed account stays alive while its own posts keep passing the likes threshold.
        db.conn.executemany(
            "UPDATE watchlist SET last_hit_at=? WHERE kind='bluesky_account' AND value=? AND active=1",
            [(ts, h) for h in seen_own_posts if h not in static],
        )
        dropped = _expire(db, "bluesky_account", cfg["ttl_days"], cfg["max_active"], now)
    return WatchChanges(added, dropped)
