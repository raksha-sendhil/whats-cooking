"""Importance scoring for topics.

Each component is in [0, 1]:
  spread      independent communities covering the topic (HN, Lobsters, newsletters, ...)
  engagement  how strong its items are *relative to their own source* (percentiles)
  velocity    how fast its items' metrics grew between collection runs
  builder     traction of related repos/models (stars/day, trending)
  curation    picked up by expert newsletters, lab blogs or tracked experts
  discussion  total comment volume
score = weighted sum x freshness x relevance (to your interests, from config/scoring.yaml).
"""

from __future__ import annotations

import bisect
import json
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from whats_cooking.config import Settings
from whats_cooking.db import DB, now_iso

log = logging.getLogger(__name__)

DEFAULTS = {
    "weights": {"spread": 0.30, "engagement": 0.25, "velocity": 0.15, "builder": 0.10, "curation": 0.12,
                "discussion": 0.08, "official": 0.15},
    "official_sources": [],
    "official_hf_trending_top": 8,
    "freshness_half_life_hours": 36,
    "window_days": 14,
    "relevance": {"floor": 0.15, "off_topic": [], "tech_anchors": []},
}

ARTIFACT_KINDS = {"repo", "model", "space", "dataset"}
PAIN_KINDS = {"ask", "issue", "question", "thread"}
CURATED_FEED_GROUPS = {"expert", "lab", "aggregator"}
_FUNDING = re.compile(r"\b(raises?|raised|funding|series [a-e]\b|seed round|valuation|acquires?|acquired|acquisition|ipo)\b", re.I)


def _g(m: dict, k: str) -> float:
    v = m.get(k)
    return float(v) if isinstance(v, (int, float)) else 0.0


def engagement_value(source: str, kind: str, m: dict, age_days: float | None) -> float | None:
    """One comparable number per item; None when the source has no engagement signal."""
    if source == "hackernews":
        return _g(m, "points") + 0.5 * _g(m, "comments")
    if source == "lobsters":
        return _g(m, "score") + 0.5 * _g(m, "comments")
    if source == "reddit":
        return max(0.0, 26 - _g(m, "day_rank")) if "day_rank" in m else None
    if source == "github":
        if kind == "issue":
            return _g(m, "thumbs_up") + 0.5 * _g(m, "comments")
        if kind == "repo":
            if "stars_today" in m:
                return _g(m, "stars_today")
            return _g(m, "stars") / max(age_days or 7.0, 0.5)   # stars/day for new repos
        return None
    if source == "huggingface":
        return _g(m, "upvotes") + _g(m, "comments") if kind == "paper" else _g(m, "trending_score")
    if source == "bluesky":
        return _g(m, "likes") + 2 * _g(m, "reposts") + 2 * _g(m, "quotes") + _g(m, "replies")
    if source == "mastodon":
        return _g(m, "accounts_2d") if kind == "link" else _g(m, "favourites") + 2 * _g(m, "reblogs") + _g(m, "replies")
    if source == "devto":
        return _g(m, "reactions") + _g(m, "comments")
    if source == "discourse":
        return _g(m, "likes") + 2 * _g(m, "replies") + _g(m, "views") / 50
    if source == "stackexchange":
        return 3 * _g(m, "score") + _g(m, "answers") + _g(m, "views") / 100
    if source == "producthunt":
        return _g(m, "votes") + _g(m, "comments") if "votes" in m else None
    return None


# Cumulative counters whose growth between snapshots is meaningful.
VELOCITY_METRIC = {
    "hackernews": "points", "lobsters": "score", "github": "stars", "huggingface": "likes",
    "bluesky": "likes", "mastodon": "favourites", "devto": "reactions", "discourse": "likes",
    "stackexchange": "score", "producthunt": "votes",
}


def comment_count(m: dict) -> float:
    return sum(_g(m, k) for k in ("comments", "replies", "answers"))


def source_group(source: str, kind: str, extra: dict) -> str:
    """Independent communities. HF daily papers and arXiv are one research pipeline."""
    if source == "rss":
        return f"rss:{extra.get('feed_group') or 'other'}"
    if kind == "paper":
        return "papers"
    return source


def topic_type(kinds: set[str], titles: list[str]) -> str:
    if any(_FUNDING.search(t) for t in titles):
        return "funding"
    if kinds & {"release", "model", "space", "dataset"}:
        return "release"
    if kinds & {"repo", "show", "launch"}:
        return "tool"
    if "paper" in kinds and len(kinds) == 1:
        return "research"
    if kinds and kinds <= PAIN_KINDS:
        return "pain"
    if "paper" in kinds:
        return "research"
    return "news"


@dataclass
class TopicScore:
    topic_id: int
    score: float
    features: dict


class _Percentiles:
    def __init__(self):
        self.values: dict[str, list[float]] = defaultdict(list)

    def add(self, key: str, v: float) -> None:
        self.values[key].append(v)

    def freeze(self) -> None:
        for k in self.values:
            self.values[k].sort()

    def pct(self, key: str, v: float) -> float:
        vals = self.values.get(key)
        if not vals or len(vals) < 3:
            return 0.5
        return bisect.bisect_right(vals, v) / len(vals)


def run_score(settings: Settings, db: DB, embedder=None) -> list[TopicScore]:
    cfg = {**DEFAULTS, **(settings.scoring or {})}
    weights = {**DEFAULTS["weights"], **(cfg.get("weights") or {})}
    source_w = {"default": 0.5, **(cfg.get("source_weights") or {})}
    now = datetime.now(UTC)
    window_start = (now - timedelta(days=cfg["window_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.conn

    rows = conn.execute(
        """
        SELECT ti.topic_id, it.id, it.source, it.kind, it.channel, it.title, it.metrics, it.extra,
               CASE WHEN it.kind IN ('repo', 'model', 'space', 'dataset') THEN it.first_seen_at
                    ELSE COALESCE(it.created_at, it.first_seen_at) END AS ts
        FROM topic_items ti
        JOIN items it ON it.id = ti.item_id
        JOIN topics t ON t.id = ti.topic_id
        WHERE t.merged_into IS NULL AND t.last_seen_at >= ?
        """,
        (window_start,),
    ).fetchall()
    if not rows:
        return []

    velocity = _velocities(conn, now)
    eng_pct, vel_pct = _Percentiles(), _Percentiles()
    parsed = []
    for r in rows:
        m, extra = json.loads(r["metrics"]), json.loads(r["extra"])
        created = _parse(r["ts"])
        age_days = (now - created).total_seconds() / 86400 if created else None
        e = engagement_value(r["source"], r["kind"], m, age_days)
        key = f"{r['source']}:{r['kind']}"
        if e is not None:
            eng_pct.add(key, e)
        v = velocity.get(r["id"])
        if v is not None:
            vel_pct.add(r["source"], v)
        parsed.append((r, m, extra, created, e, v, key))
    eng_pct.freeze()
    vel_pct.freeze()

    tracked_bsky = set(settings.source_cfg("bluesky").get("accounts", [])) | set(db.watchlist("bluesky_account"))
    official_channels = set(cfg.get("official_sources") or [])
    hf_top_threshold = {}
    for kind in ("model", "space"):
        vals = sorted((m.get("trending_score") or 0 for r, m, *_ in parsed
                       if r["source"] == "huggingface" and r["kind"] == kind), reverse=True)
        n = int(cfg.get("official_hf_trending_top", 8))
        if len(vals) >= n:
            hf_top_threshold[kind] = vals[n - 1]
    by_topic: dict[int, list] = defaultdict(list)
    for p in parsed:
        by_topic[p[0]["topic_id"]].append(p)

    relevance = _relevance(conn, list(by_topic), cfg.get("relevance") or {}, settings, embedder)
    half_life = float(cfg["freshness_half_life_hours"])
    scores: list[TopicScore] = []
    for tid, members in by_topic.items():
        groups = {source_group(r["source"], r["kind"], extra) for r, _, extra, *_ in members}
        w = lambda r, extra: source_w.get(source_group(r["source"], r["kind"], extra), source_w["default"])
        vel = [vel_pct.pct(r["source"], v) * w(r, extra) for r, _, extra, _, _, v, _ in members if v is not None]
        # Artifacts (repos/models) feed "builder"; everything else feeds "engagement". No double counting.
        # Each item's strength within its own source is scaled by how much that community counts.
        builder = [eng_pct.pct(key, e) * w(r, extra) for r, _, extra, _, e, _, key in members
                   if e is not None and r["kind"] in ARTIFACT_KINDS]
        eng = sorted((eng_pct.pct(key, e) * w(r, extra) for r, _, extra, _, e, _, key in members
                      if e is not None and r["kind"] not in ARTIFACT_KINDS), reverse=True)
        # Spread = the weight of every community beyond the strongest one: HN + Reddit counts far more
        # than Bluesky + Mastodon.
        gw = sorted((source_w.get(g, source_w["default"]) for g in groups), reverse=True)
        spread = min(sum(gw[1:]), 3.0) / 3.0
        curated = sum(
            1 for r, _, extra, *_ in members
            if (r["source"] == "rss" and extra.get("feed_group") in CURATED_FEED_GROUPS)
            or r["source"] == "pages"
            or (r["source"] == "bluesky" and (r["channel"].removeprefix("repost:") in tracked_bsky))
        )
        # Official: a frontier lab announced it itself, or it's at the very top of Hugging Face trending.
        official = any(
            r["channel"] in official_channels
            or (r["source"] == "huggingface" and r["kind"] in ("model", "space")
                and (m.get("trending_score") or 0) >= hf_top_threshold.get(r["kind"], float("inf")))
            for r, m, *_ in members)
        comments = sum(comment_count(m) for _, m, *_ in members)
        newest = max((p[3] for p in members if p[3] is not None), default=None)
        age_h = (now - newest).total_seconds() / 3600 if newest else 72.0

        comp = {
            "spread": spread,
            "engagement": (0.7 * eng[0] + 0.3 * float(np.mean(eng[:3]))) if eng else 0.0,
            "velocity": max(vel) if vel else 0.0,
            "builder": max(builder) if builder else 0.0,
            "curation": min(curated, 3) / 3,
            "discussion": min(math.log1p(comments) / math.log1p(500), 1.0),
            "official": 1.0 if official else 0.0,
        }
        base = sum(weights[k] * comp[k] for k in weights)
        fresh = 0.5 ** (max(age_h, 0.0) / half_life)
        rel = relevance.get(tid, 1.0)
        kinds = {r["kind"] for r, *_ in members}
        features = {
            **{k: round(v, 3) for k, v in comp.items()},
            "base": round(base, 4),
            "freshness": round(fresh, 3),
            "relevance": round(rel, 3),
            "groups": sorted(groups),
            "items": len(members),
            "comments": int(comments),
            "age_hours": round(age_h, 1),
            "type": topic_type(kinds, [r["title"] for r, *_ in members]),
            "has_pain": bool(kinds & PAIN_KINDS),
        }
        scores.append(TopicScore(tid, base * fresh * rel, features))

    ts = now_iso()
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO topic_scores (topic_id, scored_at, score, features) VALUES (?,?,?,?)",
            [(s.topic_id, ts, round(s.score, 6), json.dumps(s.features)) for s in scores],
        )
    scores.sort(key=lambda s: -s.score)
    return scores


def latest_scores(db: DB, limit: int | None = None) -> list[TopicScore]:
    """Most recent score of each live topic, best first."""
    row = db.conn.execute("SELECT MAX(scored_at) FROM topic_scores").fetchone()
    if not row or not row[0]:
        return []
    q = """
        SELECT s.topic_id, s.score, s.features FROM topic_scores s
        JOIN topics t ON t.id = s.topic_id
        WHERE s.scored_at = ? AND t.merged_into IS NULL
        ORDER BY s.score DESC
    """ + (f" LIMIT {int(limit)}" if limit else "")
    return [TopicScore(r[0], r[1], json.loads(r[2])) for r in db.conn.execute(q, (row[0],))]


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _velocities(conn, now: datetime) -> dict[str, float]:
    """Per-hour growth of each item's cumulative metric over (up to) the last 30 hours."""
    since = (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    first: dict[str, tuple[datetime, float]] = {}
    last: dict[str, tuple[datetime, float]] = {}
    rows = conn.execute(
        """
        SELECT s.item_id, s.taken_at, s.metrics, it.source FROM metric_snapshots s
        JOIN items it ON it.id = s.item_id
        WHERE s.taken_at >= ? ORDER BY s.taken_at
        """,
        (since,),
    )
    for item_id, taken_at, metrics, source in rows:
        key = VELOCITY_METRIC.get(source)
        if not key:
            continue
        v = json.loads(metrics).get(key)
        if not isinstance(v, (int, float)):
            continue
        t = _parse(taken_at)
        first.setdefault(item_id, (t, float(v)))
        last[item_id] = (t, float(v))
    out = {}
    for item_id, (t0, v0) in first.items():
        t1, v1 = last[item_id]
        hours = (t1 - t0).total_seconds() / 3600
        if hours >= 1:
            out[item_id] = max(0.0, (v1 - v0) / hours)
    return out


def _relevance(conn, topic_ids: list[int], cfg: dict, settings: Settings, embedder) -> dict[int, float]:
    """Off-topic filter as a multiplier in [floor, 1].

    Small embedding models match short tech titles to broad "interest" phrases poorly, but they
    recognise general news (politics, crime, sports...) reliably. So only demote topics that are
    clearly closer to an off-topic phrase than to any tech anchor; judging what *you* care about
    is left to Claude, using config/profile.md.
    """
    off_topic, anchors = cfg.get("off_topic") or [], cfg.get("tech_anchors") or []
    if not off_topic or not topic_ids:
        return {}
    if embedder is None:
        from whats_cooking.pipeline.group import DEFAULTS as GDEF, Embedder
        model = (settings.grouping or {}).get("model", GDEF["model"])
        embedder = Embedder(model, cache_dir=str(settings.root / "data" / "models"))
    neg = embedder.embed(list(off_topic))
    pos = embedder.embed(list(anchors)) if anchors else None
    floor = float(cfg.get("floor", 0.15))
    min_sim = float(cfg.get("off_topic_min_similarity", 0.25))
    gap = float(cfg.get("off_topic_gap", 0.05))
    out: dict[int, float] = {}
    for i in range(0, len(topic_ids), 900):
        chunk = topic_ids[i:i + 900]
        q = f"SELECT id, centroid FROM topics WHERE id IN ({','.join('?' * len(chunk))}) AND centroid IS NOT NULL"
        for tid, blob in conn.execute(q, chunk):
            c = np.frombuffer(blob, dtype=np.float32)
            n = float((neg @ c).max())
            p = float((pos @ c).max()) if pos is not None else 0.0
            # Both must hold: clearly off-topic in absolute terms, and more off-topic than tech.
            penalty = float(np.clip((n - p - gap) / 0.2, 0, 1) * np.clip((n - min_sim) / 0.15, 0, 1))
            out[tid] = 1.0 - (1.0 - floor) * penalty
    return out
