"""Group items from all sources into topics ("the same story / the same thing").

An item joins an existing topic when:
  1. Hard link: it shares a ref (arxiv id, repo, canonical URL...) with an item in the topic, or
  2. Soft link: it is close to some item in the topic by BOTH embedding similarity and
     overlap of distinctive title words (IDF-weighted). Embeddings alone can't tell
     "same story" from "same theme"; rare shared words like "EmbeddingGemma" can.
Artifacts (papers, repos, models...) are distinct things, so two artifacts only merge when
near-identical. If an item links to several topics, they're the same story: merge them.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from whats_cooking.config import Settings
from whats_cooking.db import DB, now_iso

log = logging.getLogger(__name__)

ARTIFACT_KINDS = {"paper", "repo", "model", "space", "dataset", "release"}

DEFAULTS = {
    "model": "sentence-transformers/all-MiniLM-L6-v2",
    "window_days": 14,
    "hub_ref_limit": 5,
    "text_ref_sources": ["bluesky", "mastodon"],
    "embed_text_chars": 160,
    "embed_batch": 32,
    "candidates": 30,
    "max_bridge": 3,
    # Each rule: join if cos >= min_cos AND overlap >= min_overlap (any rule suffices).
    "rules": {
        "discussion": [[0.90, 0.0], [0.78, 0.2], [0.65, 0.5], [0.55, 0.65]],
        "artifact_discussion": [[0.85, 0.4], [0.62, 0.6]],
        "paper_discussion": [[0.80, 0.45]],
        "artifact_pair": [[0.92, 0.5]],
    },
}

# Penalties when choosing a topic's display title (social snippets and repo slugs read worse).
TITLE_PENALTY = {"bluesky": 0.06, "mastodon": 0.06, "reddit": 0.03, "github": 0.04, "huggingface": 0.04}

# Which item title best names a topic, when several sources cover it.
TITLE_PRIORITY = ["rss", "pages", "hackernews", "lobsters", "huggingface", "arxiv", "github",
                  "producthunt", "devto", "discourse", "stackexchange", "reddit", "mastodon", "bluesky"]

_STOP = set(
    "the a an and or of to in for on with by from at is are be as it its this that new how why what your you we our "
    "using use via vs into than more about after over can will not just now all has have get got out who his her "
    "their they them was were been being here there when where which while also only one two first introducing "
    "launches launch released releases release announces announced show ask hn".split()
)
_TOKEN = re.compile(r"[a-z0-9][a-z0-9.]*[a-z0-9]|[a-z0-9]")


def title_tokens(title: str) -> frozenset[str]:
    return frozenset(w for w in _TOKEN.findall(title.lower()) if len(w) > 2 and w not in _STOP)


# Release names: a capitalised word joined to a version number, optionally with a following name word,
# e.g. "Gemini 4 Argon", "Grok Voice 3.0", "MAI-Base-2", "Python 3.15", "Nemotron-3.5". The digit is
# what makes it specific: "Claude Code" alone spans many unrelated stories, "Claude 5.5" is one release.
_ENTITY = re.compile(
    r"\b([A-Z][A-Za-z]{2,}(?:[ -][A-Z][A-Za-z]+)?[ -]?v?\d+(?:\.\d+)*(?:[ -](?:[A-Z][a-z]{2,}|Pro|Max|Mini|Flash|Ultra))?)\b"
    r"|\b([A-Z]{2,}[A-Za-z]*-[A-Za-z]+-\d+(?:\.\d+)?)\b")


_NOT_A_NAME = {"top", "the", "my", "our", "how", "why", "what", "part", "step", "day", "days", "week", "weeks",
               "year", "years", "episode", "issue", "version", "chapter", "level", "phase", "season", "vol", "volume",
               "number", "page", "series", "round", "gen", "generation", "rank", "stage", "tier", "over", "under",
               "only", "first", "last", "best", "with", "and", "for", "from", "after", "before", "about", "introducing",
               "announcing", "meet", "new", "release", "releases", "released", "launching", "unveils"}
_VENDORS = {"google", "openai", "anthropic", "meta", "microsoft", "nvidia", "apple", "amazon", "aws", "mistral",
            "deepseek", "alibaba", "baidu", "tencent", "xai", "ibm", "intel", "amd", "qualcomm", "samsung", "hugging",
            "cohere", "stability", "moonshot", "zhipu", "minimax", "bytedance", "spacexai"}


def release_names(title: str) -> set[str]:
    out = set()
    for m in _ENTITY.finditer(title or ""):
        name = re.sub(r"[\s-]+", " ", (m.group(1) or m.group(2))).strip().lower()
        words = name.split()
        # Drop a leading filler word or company name, so "Introducing Gemini 4 Argon" and "NVIDIA Nemotron 3.5"
        # line up with "Gemini 4 Argon" and "Nemotron 3.5" (but "MAI Base 2" never shrinks to "Base 2").
        variants = [words] + ([words[1:]] if words and words[0] in _NOT_A_NAME | _VENDORS else [])
        for variant in variants:
            if len(variant) < 2 or variant[0] in _NOT_A_NAME or not re.fullmatch(r"[a-z][a-z.]{2,}", variant[0]):
                continue
            v = " ".join(variant)
            nums = re.findall(r"\d+(?:\.\d+)*", v)
            # Versions look like "4", "3.0" or "3.15"; "Commodore 64", "Windows 95" or "October 2026" aren't.
            if nums and not any("." not in x and int(x) >= 20 for x in nums):
                out.add(v)
    return out


class Embedder:
    def __init__(self, model_name: str, cache_dir: str | None = None):
        from fastembed import TextEmbedding  # heavy import, only when grouping

        self.name = model_name
        self.model = TextEmbedding(model_name, cache_dir=cache_dir)

    def embed(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        vecs = np.array(list(self.model.embed(texts, batch_size=batch_size)), dtype=np.float32)
        return _normalize(vecs)


def _normalize(v: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(norms, 1e-12)


@dataclass
class GroupStats:
    embedded: int = 0
    assigned: int = 0
    by_ref: int = 0
    by_similarity: int = 0
    new_topics: int = 0
    merged_topics: int = 0
    skipped_baseline: int = 0


@dataclass
class _Topic:
    id: int
    centroid: np.ndarray
    count: int
    dirty: bool = False
    merged_into: int | None = None
    refs: set[str] = field(default_factory=set)


class _ItemIndex:
    """Growable matrix of item vectors (with kind, tokens and topic) for nearest-item lookup."""

    def __init__(self, dim: int):
        self.mat = np.zeros((4096, dim), dtype=np.float32)
        self.n = 0
        self.kinds: list[str] = []
        self.tokens: list[frozenset[str]] = []
        self.topic: list[int] = []

    def add(self, vec: np.ndarray, kind: str, tokens: frozenset[str], topic_id: int) -> None:
        if self.n == len(self.mat):
            self.mat = np.vstack([self.mat, np.zeros_like(self.mat)])
        self.mat[self.n] = vec
        self.kinds.append(kind)
        self.tokens.append(tokens)
        self.topic.append(topic_id)
        self.n += 1

    def candidates(self, vec: np.ndarray, floor: float, k: int) -> list[tuple[int, float]]:
        if self.n == 0:
            return []
        sims = self.mat[: self.n] @ vec
        idx = np.nonzero(sims >= floor)[0]
        if len(idx) > k:
            idx = idx[np.argpartition(-sims[idx], k)[:k]]
        return sorted(((int(i), float(sims[i])) for i in idx), key=lambda x: -x[1])


class _Matcher:
    def __init__(self, rules: dict, idf: dict[str, float], default_idf: float):
        self.rules = rules
        self.idf = idf
        self.default_idf = default_idf
        self.floor = min(r[0] for rs in rules.values() for r in rs)

    def overlap(self, a: frozenset[str], b: frozenset[str]) -> float:
        """IDF-weighted share of the shorter title's words found in the other title.

        The denominator has a floor so a title made only of common words ("a new model")
        can't count as a full match.
        """
        shared = a & b
        if not shared:
            return 0.0
        w = lambda toks: sum(self.idf.get(t, self.default_idf) for t in toks)
        return w(shared) / max(min(w(a), w(b)), 0.75 * self.default_idf)

    def rule_set(self, kind_a: str, kind_b: str) -> list:
        art_a, art_b = kind_a in ARTIFACT_KINDS, kind_b in ARTIFACT_KINDS
        if art_a and art_b:
            return self.rules["artifact_pair"]
        if "paper" in (kind_a, kind_b):
            return self.rules["paper_discussion"]
        if art_a or art_b:
            return self.rules["artifact_discussion"]
        return self.rules["discussion"]

    def matches(self, cos: float, kind_a: str, tok_a, kind_b: str, tok_b) -> bool:
        rules = self.rule_set(kind_a, kind_b)
        if cos < min(r[0] for r in rules):
            return False
        ov = self.overlap(tok_a, tok_b)
        return any(cos >= c and ov >= o for c, o in rules)


def run_group(settings: Settings, db: DB, embedder: Embedder | None = None) -> GroupStats:
    cfg = {**DEFAULTS, **(settings.grouping or {})}
    cfg["rules"] = {**DEFAULTS["rules"], **(cfg.get("rules") or {})}
    stats = GroupStats()
    window_start = (datetime.now(UTC) - timedelta(days=cfg["window_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = db.conn

    # 1. Embed items without a vector from the current model.
    pending = conn.execute(
        """
        SELECT i.id, i.title, i.text FROM items i
        LEFT JOIN embeddings e ON e.item_id = i.id
        WHERE (e.item_id IS NULL OR e.model != ?) AND i.first_seen_at >= ? AND i.kind NOT IN ('job', 'request')
        """,
        (cfg["model"], window_start),
    ).fetchall()
    if pending:
        embedder = embedder or Embedder(cfg["model"], cache_dir=str(settings.root / "data" / "models"))
        texts = [_embed_text(r["title"], r["text"], cfg["embed_text_chars"]) for r in pending]
        vecs = embedder.embed(texts, batch_size=cfg["embed_batch"])
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings (item_id, model, vector) VALUES (?,?,?)",
                [(r["id"], cfg["model"], v.tobytes()) for r, v in zip(pending, vecs)],
            )
        stats.embedded = len(pending)

    # 2. Items that still need a topic (oldest first, so topics grow in time order).
    todo = conn.execute(
        """
        SELECT i.id, i.kind, i.title, i.extra, e.vector
        FROM items i
        JOIN embeddings e ON e.item_id = i.id AND e.model = ?
        LEFT JOIN topic_items ti ON ti.item_id = i.id
        WHERE ti.item_id IS NULL AND i.first_seen_at >= ? AND i.kind NOT IN ('job', 'request')
        ORDER BY COALESCE(i.created_at, i.first_seen_at), i.id
        """,
        (cfg["model"], window_start),
    ).fetchall()
    if not todo:
        return stats

    dim = len(np.frombuffer(todo[0]["vector"], dtype=np.float32))
    _touch_active_topics(conn, window_start)
    topics, ref_index = _load_active_topics(conn, window_start)
    index = _load_item_index(conn, window_start, cfg["model"], dim, topics)
    matcher = _Matcher(cfg["rules"], *_idf(conn, window_start))
    hub = _hub_refs(conn, cfg["hub_ref_limit"])
    item_refs = _refs_for(conn, [r["id"] for r in todo], set(cfg["text_ref_sources"]))
    next_id = (conn.execute("SELECT COALESCE(MAX(id), 0) FROM topics").fetchone()[0]) + 1
    ts = now_iso()
    assignments: list[tuple] = []

    for row in todo:
        if json.loads(row["extra"] or "{}").get("baseline"):
            stats.skipped_baseline += 1
            continue
        vec = np.frombuffer(row["vector"], dtype=np.float32)
        kind, tokens = row["kind"], title_tokens(row["title"])
        refs = item_refs.get(row["id"], set()) - hub

        ref_topics = {_resolve(topics, ref_index[r]) for r in refs if r in ref_index}
        sim_topics: dict[int, float] = {}
        for i, cos in index.candidates(vec, matcher.floor, cfg["candidates"]):
            if matcher.matches(cos, kind, tokens, index.kinds[i], index.tokens[i]):
                t = _resolve(topics, index.topic[i])
                sim_topics[t] = max(cos, sim_topics.get(t, -1.0))
        if len(ref_topics | set(sim_topics)) > cfg["max_bridge"]:
            # Too many matches to trust as one story: keep ref links + the single best similarity match.
            best = max(sim_topics, key=sim_topics.get) if sim_topics else None
            sim_topics = {best: sim_topics[best]} if best is not None else {}

        linked = ref_topics | set(sim_topics)
        if linked:
            target = max(linked, key=lambda t: topics[t].count)
            for other in linked - {target}:
                _merge(topics, ref_index, other, target)
                stats.merged_topics += 1
            method = "ref" if ref_topics else "similarity"
            sim = sim_topics.get(target, max(sim_topics.values(), default=None))
            if ref_topics:
                stats.by_ref += 1
            else:
                stats.by_similarity += 1
        else:
            target, method, sim = next_id, "new", None
            next_id += 1
            topics[target] = _Topic(target, vec.copy(), 0, dirty=True)
            stats.new_topics += 1

        t = topics[target]
        t.centroid = _normalize(t.centroid * t.count + vec) if t.count else vec.copy()
        t.count += 1
        t.dirty = True
        for r in refs:
            ref_index[r] = target
            t.refs.add(r)
        index.add(vec, kind, tokens, target)
        assignments.append((row["id"], target, method, sim, ts))
        stats.assigned += 1

    _persist(conn, topics, assignments)
    stats.merged_topics += merge_by_release_name(conn, window_start, cfg.get("release_name_max_items", 40),
                                                 cfg.get("release_name_fresh_days", 5))
    return stats


def merge_by_release_name(conn, window_start: str, max_items: int = 40, fresh_days: float = 5) -> int:
    """Merge topics that share a specific, NEW release name ("Gemini 4 Argon") into one.

    A name counts only if it first appeared within `fresh_days` (a fresh release, not a model people
    have discussed for weeks, like "Opus 5.5") and at most `max_items` items mention it. Topics made only
    of papers, or only of repos/models, don't merge with each other on a name alone.
    """
    rows = conn.execute(
        """
        SELECT ti.topic_id, it.kind, it.title, COALESCE(it.created_at, it.first_seen_at) AS ts
        FROM topic_items ti JOIN items it ON it.id = ti.item_id
        JOIN topics t ON t.id = ti.topic_id
        WHERE t.merged_into IS NULL AND it.first_seen_at >= ?
        """,
        (window_start,),
    ).fetchall()
    fresh_since = (datetime.now(UTC) - timedelta(days=fresh_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_name: dict[str, set[int]] = {}
    mentions: Counter[str] = Counter()
    first_mention: dict[str, str] = {}
    kinds: dict[int, set[str]] = {}
    for tid, kind, title, ts in rows:
        kinds.setdefault(tid, set()).add(kind)
        for name in release_names(title):
            by_name.setdefault(name, set()).add(tid)
            mentions[name] += 1
            if ts and (name not in first_mention or ts < first_mention[name]):
                first_mention[name] = ts
    for name in [n for n, ts in first_mention.items() if ts < fresh_since]:
        by_name.pop(name, None)   # an established name, not a new release
    merged, parent = 0, {}

    def find(t):
        while parent.get(t, t) != t:
            t = parent[t]
        return t

    sizes = dict(conn.execute("SELECT id, item_count FROM topics WHERE merged_into IS NULL"))
    for name, tids in sorted(by_name.items(), key=lambda kv: -len(kv[1])):
        if mentions[name] > max_items or len(tids) < 2 or len(tids) > 8:
            continue
        groups = {find(t) for t in tids}
        # A name alone links stories/posts/threads; it never joins two paper-only or two artifact-only topics.
        discussion = [t for t in groups if kinds.get(t, set()) - ARTIFACT_KINDS]
        if not discussion:
            continue
        keep = max(groups, key=lambda t: sizes.get(t, 0))
        for t in groups - {keep}:
            if not (kinds.get(t, set()) - ARTIFACT_KINDS) and not (kinds.get(keep, set()) - ARTIFACT_KINDS):
                continue
            merge_topics(conn, t, keep)
            parent[t] = keep
            sizes[keep] = sizes.get(keep, 0) + sizes.get(t, 0)
            kinds.setdefault(keep, set()).update(kinds.get(t, set()))
            merged += 1
    return merged


def merge_topics(conn, src: int, dst: int) -> None:
    """Fold topic `src` into `dst` (used when a later check finds they're the same story)."""
    if src == dst:
        return
    with conn:
        conn.execute("UPDATE topic_items SET topic_id=?, method='merged' WHERE topic_id=?", (dst, src))
        conn.execute("UPDATE topics SET merged_into=? WHERE id=?", (dst, src))
        vecs = [np.frombuffer(r[0], dtype=np.float32) for r in conn.execute(
            "SELECT e.vector FROM topic_items ti JOIN embeddings e ON e.item_id = ti.item_id WHERE ti.topic_id=?",
            (dst,))]
        if vecs:
            centroid = _normalize(np.mean(np.stack(vecs), axis=0))
            conn.execute("UPDATE topics SET centroid=? WHERE id=?", (centroid.astype(np.float32).tobytes(), dst))
        _refresh_topic_summaries(conn, [dst])


def _embed_text(title: str, text: str, chars: int) -> str:
    text = (text or "").strip()
    return f"{title.strip()}. {text[:chars]}" if text else title.strip()


def _resolve(topics: dict[int, _Topic], tid: int) -> int:
    while topics[tid].merged_into is not None:
        tid = topics[tid].merged_into
    return tid


def _merge(topics, ref_index: dict, src: int, dst: int) -> None:
    a, b = topics[src], topics[dst]
    b.centroid = _normalize(a.centroid * a.count + b.centroid * b.count)
    b.count += a.count
    b.refs |= a.refs
    b.dirty = True
    for r in a.refs:
        ref_index[r] = dst
    a.merged_into = dst
    a.dirty = True


def _touch_active_topics(conn, window_start: str) -> None:
    """A topic stays active while any of its items is still being seen by collectors."""
    with conn:
        conn.execute(
            """
            UPDATE topics SET last_seen_at = (
                SELECT MAX(it.last_seen_at) FROM topic_items ti JOIN items it ON it.id = ti.item_id
                WHERE ti.topic_id = topics.id)
            WHERE merged_into IS NULL AND id IN (
                SELECT ti.topic_id FROM topic_items ti JOIN items it ON it.id = ti.item_id
                WHERE it.last_seen_at >= ?)
            """,
            (window_start,),
        )


def _load_active_topics(conn, window_start: str):
    topics: dict[int, _Topic] = {}
    for r in conn.execute(
        "SELECT id, centroid, item_count FROM topics WHERE merged_into IS NULL AND last_seen_at >= ? AND centroid IS NOT NULL",
        (window_start,),
    ):
        topics[r["id"]] = _Topic(r["id"], np.frombuffer(r["centroid"], dtype=np.float32).copy(), r["item_count"])
    ref_index: dict[str, int] = {}
    if topics:
        rows = conn.execute(
            """
            SELECT ir.ref, ti.topic_id FROM item_refs ir
            JOIN topic_items ti ON ti.item_id = ir.item_id
            WHERE ir.primary_ref = 1
            """
        )
        for ref, tid in rows:
            if tid in topics:
                ref_index[ref] = tid
                topics[tid].refs.add(ref)
    return topics, ref_index


def _load_item_index(conn, window_start: str, model: str, dim: int, topics: dict[int, _Topic]) -> _ItemIndex:
    index = _ItemIndex(dim)
    rows = conn.execute(
        """
        SELECT it.kind, it.title, e.vector, ti.topic_id FROM topic_items ti
        JOIN items it ON it.id = ti.item_id
        JOIN embeddings e ON e.item_id = ti.item_id AND e.model = ?
        WHERE it.first_seen_at >= ?
        """,
        (model, window_start),
    )
    for r in rows:
        if r["topic_id"] in topics:
            index.add(np.frombuffer(r["vector"], dtype=np.float32), r["kind"], title_tokens(r["title"]), r["topic_id"])
    return index


def _idf(conn, window_start: str) -> tuple[dict[str, float], float]:
    """IDF of title words over the current window: rare words carry the identity of a story."""
    df: Counter[str] = Counter()
    n = 0
    for (title,) in conn.execute("SELECT title FROM items WHERE first_seen_at >= ?", (window_start,)):
        df.update(title_tokens(title))
        n += 1
    n = max(n, 1)
    return {w: math.log(n / (1 + c)) + 1.0 for w, c in df.items()}, math.log(n) + 1.0


def _hub_refs(conn, limit: int) -> set[str]:
    rows = conn.execute(
        """
        SELECT ir.ref FROM item_refs ir JOIN topic_items ti ON ti.item_id = ir.item_id
        GROUP BY ir.ref HAVING COUNT(DISTINCT ti.topic_id) > ?
        """,
        (limit,),
    )
    return {r[0] for r in rows}


def _refs_for(conn, item_ids: list[str], text_ref_sources: set[str]) -> dict[str, set[str]]:
    """Refs usable for hard-linking: primary refs, plus the single text ref of short social posts."""
    out: dict[str, set[str]] = {}
    text_refs: dict[str, set[str]] = {}
    for i in range(0, len(item_ids), 500):
        chunk = item_ids[i:i + 500]
        q = f"""
            SELECT ir.item_id, ir.ref, ir.primary_ref, it.source FROM item_refs ir
            JOIN items it ON it.id = ir.item_id
            WHERE ir.item_id IN ({','.join('?' * len(chunk))})
        """
        for item_id, ref, primary, source in conn.execute(q, chunk):
            if primary:
                out.setdefault(item_id, set()).add(ref)
            elif source in text_ref_sources:
                text_refs.setdefault(item_id, set()).add(ref)
    for item_id, refs in text_refs.items():
        if len(refs) == 1 and item_id not in out:
            out[item_id] = refs
    return out


def _persist(conn, topics: dict[int, _Topic], assignments: list[tuple]) -> None:
    with conn:
        for t in topics.values():
            if not t.dirty:
                continue
            conn.execute(
                """
                INSERT INTO topics (id, title, first_seen_at, last_seen_at, item_count, centroid, merged_into)
                VALUES (?, '', ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET item_count=excluded.item_count, centroid=excluded.centroid,
                                              merged_into=excluded.merged_into
                """,
                (t.id, now_iso(), now_iso(), t.count, t.centroid.astype(np.float32).tobytes(), t.merged_into),
            )
        conn.executemany(
            "INSERT OR REPLACE INTO topic_items (item_id, topic_id, method, similarity, added_at) VALUES (?,?,?,?,?)",
            assignments,
        )
        # Re-point items of merged topics to the surviving topic.
        for t in topics.values():
            if t.merged_into is not None:
                conn.execute("UPDATE topic_items SET topic_id=? WHERE topic_id=?", (_resolve(topics, t.id), t.id))
        _refresh_topic_summaries(conn, [_resolve(topics, t.id) for t in topics.values() if t.dirty])


def _refresh_topic_summaries(conn, topic_ids: list[int]) -> None:
    """Recompute title, sources, counts and time span from member items."""
    for tid in set(topic_ids):
        centroid_blob = conn.execute("SELECT centroid FROM topics WHERE id=?", (tid,)).fetchone()[0]
        rows = conn.execute(
            """
            SELECT it.source, it.title, COALESCE(it.created_at, it.first_seen_at) AS ts, it.last_seen_at, e.vector
            FROM topic_items ti JOIN items it ON it.id = ti.item_id
            LEFT JOIN embeddings e ON e.item_id = ti.item_id
            WHERE ti.topic_id = ?
            """,
            (tid,),
        ).fetchall()
        if not rows:
            continue
        sources = Counter(r["source"] for r in rows)
        centroid = np.frombuffer(centroid_blob, dtype=np.float32) if centroid_blob else None
        conn.execute(
            """
            UPDATE topics SET title=?, item_count=?, source_count=?, sources=?, first_seen_at=?, last_seen_at=?
            WHERE id=?
            """,
            (
                _best_title(rows, centroid),
                len(rows),
                len(sources),
                json.dumps(dict(sources.most_common())),
                min(r["ts"] for r in rows),
                max(r["last_seen_at"] for r in rows),
                tid,
            ),
        )


def _best_title(rows, centroid: np.ndarray | None) -> str:
    """The most representative member's title (closest to the topic centroid), preferring readable sources."""
    def rank(r):
        length_penalty = 0 if 15 <= len(r["title"]) <= 160 else 1
        if centroid is not None and r["vector"] is not None:
            rep = float(np.frombuffer(r["vector"], dtype=np.float32) @ centroid) - TITLE_PENALTY.get(r["source"], 0.0)
        else:
            rep = 0.0
        src = TITLE_PRIORITY.index(r["source"]) if r["source"] in TITLE_PRIORITY else len(TITLE_PRIORITY)
        return (length_penalty, -round(rep, 2), src)
    return min(rows, key=rank)["title"]
