"""SQLite storage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from whats_cooking.models import Item
from whats_cooking.refs import canonical_url, refs_from_text, url_ref

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id             TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    native_id      TEXT NOT NULL,
    kind           TEXT NOT NULL,
    channel        TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL,
    url            TEXT,
    canonical_url  TEXT,
    discussion_url TEXT,
    author         TEXT,
    text           TEXT NOT NULL DEFAULT '',
    created_at     TEXT,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    metrics        TEXT NOT NULL DEFAULT '{}',
    tags           TEXT NOT NULL DEFAULT '[]',
    extra          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS items_source ON items(source, channel);
CREATE INDEX IF NOT EXISTS items_first_seen ON items(first_seen_at);

-- What each item is about (arxiv:..., github:..., url:...). primary_ref = from the item's own URL.
CREATE TABLE IF NOT EXISTS item_refs (
    item_id     TEXT NOT NULL,
    ref         TEXT NOT NULL,
    primary_ref INTEGER NOT NULL,
    PRIMARY KEY (item_id, ref)
);
CREATE INDEX IF NOT EXISTS item_refs_ref ON item_refs(ref);

-- Metrics over time, one row per item per run: the basis for velocity and persistence.
CREATE TABLE IF NOT EXISTS metric_snapshots (
    item_id  TEXT NOT NULL,
    run_id   INTEGER NOT NULL,
    taken_at TEXT NOT NULL,
    metrics  TEXT NOT NULL,
    PRIMARY KEY (item_id, run_id)
);

CREATE TABLE IF NOT EXISTS embeddings (
    item_id TEXT PRIMARY KEY,
    model   TEXT NOT NULL,
    vector  BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    item_count    INTEGER NOT NULL DEFAULT 0,
    source_count  INTEGER NOT NULL DEFAULT 0,
    sources       TEXT NOT NULL DEFAULT '[]',
    centroid      BLOB,
    merged_into   INTEGER
);
CREATE INDEX IF NOT EXISTS topics_last_seen ON topics(last_seen_at);

CREATE TABLE IF NOT EXISTS topic_items (
    item_id    TEXT PRIMARY KEY,
    topic_id   INTEGER NOT NULL,
    method     TEXT NOT NULL,      -- 'ref' | 'embedding' | 'new'
    similarity REAL,
    added_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS topic_items_topic ON topic_items(topic_id);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS collector_runs (
    run_id      INTEGER NOT NULL,
    collector   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,     -- 'ok' | 'partial' | 'error' | 'skipped'
    items       INTEGER NOT NULL DEFAULT 0,
    new_items   INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    PRIMARY KEY (run_id, collector)
);

-- Importance scores per scoring run; the latest row per topic is the current score.
CREATE TABLE IF NOT EXISTS topic_scores (
    topic_id  INTEGER NOT NULL,
    scored_at TEXT NOT NULL,
    score     REAL NOT NULL,
    features  TEXT NOT NULL,
    PRIMARY KEY (topic_id, scored_at)
);
CREATE INDEX IF NOT EXISTS topic_scores_at ON topic_scores(scored_at);

-- Comments / READMEs fetched for shortlisted items only.
CREATE TABLE IF NOT EXISTS enrichments (
    item_id    TEXT PRIMARY KEY,
    comments   TEXT NOT NULL DEFAULT '[]',
    readme     TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);

-- Every Claude call: the budget guard's ledger.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                    INTEGER PRIMARY KEY,
    ts                    TEXT NOT NULL,
    purpose               TEXT NOT NULL,
    model                 TEXT NOT NULL,
    status                TEXT NOT NULL,   -- 'ok' | 'limited' | 'error' | 'skipped'
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL NOT NULL DEFAULT 0,
    duration_ms           INTEGER,
    error                 TEXT
);

CREATE TABLE IF NOT EXISTS briefs (
    day        TEXT PRIMARY KEY,           -- local date, YYYY-MM-DD
    status     TEXT NOT NULL,              -- 'done' | 'fallback' | 'pending'
    model      TEXT,
    path       TEXT,
    created_at TEXT NOT NULL,
    note       TEXT
);

CREATE TABLE IF NOT EXISTS brief_topics (
    day        TEXT NOT NULL,
    topic_id   INTEGER NOT NULL,
    score      REAL NOT NULL,
    item_count INTEGER NOT NULL,
    section    TEXT NOT NULL,              -- 'top' | 'quick'
    PRIMARY KEY (day, topic_id)
);

-- Watch lists that grow and shrink on their own (repos to track releases of, Bluesky accounts).
CREATE TABLE IF NOT EXISTS watchlist (
    kind        TEXT NOT NULL,             -- 'github_repo' | 'bluesky_account'
    value       TEXT NOT NULL,
    added_at    TEXT NOT NULL,
    last_hit_at TEXT NOT NULL,
    reason      TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (kind, value)
);

-- Complaint / need statements mined from comments, issues, forums, questions, hiring posts.
CREATE TABLE IF NOT EXISTS pain_mentions (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    item_id       TEXT NOT NULL,
    source        TEXT NOT NULL,
    community     TEXT NOT NULL,
    mention_type  TEXT NOT NULL,         -- 'user' | 'company'
    author        TEXT,
    text          TEXT NOT NULL,
    context       TEXT NOT NULL,
    url           TEXT,
    created_at    TEXT,
    weight        REAL NOT NULL,
    people        INTEGER NOT NULL,      -- how many people this mention represents (1 + upvotes etc.)
    first_seen_at TEXT NOT NULL,
    vector        BLOB,
    problem_id    INTEGER
);
CREATE INDEX IF NOT EXISTS pain_mentions_problem ON pain_mentions(problem_id);

-- Comment threads already mined, so they're only fetched again when they grow.
CREATE TABLE IF NOT EXISTS mined (
    item_id            TEXT PRIMARY KEY,
    comments_at_mining INTEGER NOT NULL,
    mined_at           TEXT NOT NULL
);

-- The long game: how often each big challenge came up on HN, per month (from HN's full-history search).
CREATE TABLE IF NOT EXISTS challenge_trends (
    challenge_id TEXT NOT NULL,
    query        TEXT NOT NULL,
    period       TEXT NOT NULL,      -- YYYY-MM
    count        INTEGER NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (challenge_id, query, period)
);

-- News topics that may move a big challenge forward, and Claude's verdict once judged.
CREATE TABLE IF NOT EXISTS challenge_matches (
    challenge_id TEXT NOT NULL,
    topic_id     INTEGER NOT NULL,
    score        REAL NOT NULL,
    similarity   REAL NOT NULL,
    keyword      TEXT NOT NULL,
    title        TEXT NOT NULL,
    sources      TEXT NOT NULL DEFAULT '[]',
    matched_at   TEXT NOT NULL,
    verdict      TEXT,               -- 'real progress' | 'small step' | 'not related' (NULL = not judged yet)
    judgement    TEXT,               -- Claude's entry (JSON)
    judged_at    TEXT,
    PRIMARY KEY (challenge_id, topic_id)
);

-- Problems = groups of mentions about the same unmet need, tracked over time.
CREATE TABLE IF NOT EXISTS problems (
    id               INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL DEFAULT 'user',   -- 'user' (complaints) | 'request' (investor/company request)
    title            TEXT NOT NULL,
    centroid         BLOB,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    mentions         INTEGER NOT NULL DEFAULT 0,
    people           INTEGER NOT NULL DEFAULT 0,
    authors          INTEGER NOT NULL DEFAULT 0,
    threads          INTEGER NOT NULL DEFAULT 0,     -- distinct threads/issues/posts it came up in
    communities      TEXT NOT NULL DEFAULT '[]',
    company_mentions INTEGER NOT NULL DEFAULT 0,
    days_active      INTEGER NOT NULL DEFAULT 0,
    score            REAL NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'open',   -- 'open' | 'dismissed'
    card             TEXT,                           -- Claude-written analysis (JSON)
    card_at          TEXT,
    card_people      INTEGER
);
"""


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean(d: dict) -> dict:
    # json_patch treats null as "delete key", so never store None values.
    return {k: v for k, v in d.items() if v is not None}


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    # Columns added after a table first shipped: (table, column, definition).
    _ADDED_COLUMNS = [("problems", "kind", "TEXT NOT NULL DEFAULT 'user'"),
                      ("problems", "threads", "INTEGER NOT NULL DEFAULT 0"),
                      ("briefs", "data", "TEXT"),   # the edition as structured JSON, for the reader UI
                      ("pain_mentions", "signal", "TEXT"),           # 'workaround' when people already hack around it
                      ("problems", "workarounds", "INTEGER NOT NULL DEFAULT 0")]

    def _migrate(self) -> None:
        for table, column, definition in self._ADDED_COLUMNS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self.conn.close()

    # ---- runs -------------------------------------------------------------

    def start_run(self, kind: str) -> int:
        cur = self.conn.execute("INSERT INTO runs(kind, started_at) VALUES (?, ?)", (kind, now_iso()))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int) -> None:
        self.conn.execute("UPDATE runs SET finished_at=? WHERE id=?", (now_iso(), run_id))
        self.conn.commit()

    def record_collector(self, run_id: int, collector: str, started_at: str, status: str,
                         items: int = 0, new_items: int = 0, error: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO collector_runs VALUES (?,?,?,?,?,?,?,?)",
            (run_id, collector, started_at, now_iso(), status, items, new_items, error),
        )
        self.conn.commit()

    def known_channels(self) -> set[tuple[str, str]]:
        return {(r[0], r[1]) for r in self.conn.execute("SELECT DISTINCT source, channel FROM items")}

    def watchlist(self, kind: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT value FROM watchlist WHERE kind=? AND active=1 ORDER BY value", (kind,))]

    # ---- items ------------------------------------------------------------

    def upsert_items(self, items: Iterable[Item], run_id: int) -> tuple[int, int]:
        """Insert or update items. Returns (total, new)."""
        items = list(items)
        if not items:
            return 0, 0
        ts = now_iso()
        ids = [i.id for i in items]
        existing: set[str] = set()
        for chunk in _chunks(ids, 500):
            q = f"SELECT id FROM items WHERE id IN ({','.join('?' * len(chunk))})"
            existing.update(r[0] for r in self.conn.execute(q, chunk))

        with self.conn:
            for it in items:
                metrics = json.dumps(_clean(it.metrics))
                self.conn.execute(
                    """
                    INSERT INTO items (id, source, native_id, kind, channel, title, url, canonical_url,
                                       discussion_url, author, text, created_at, first_seen_at, last_seen_at,
                                       metrics, tags, extra)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET
                        title          = excluded.title,
                        url            = COALESCE(excluded.url, items.url),
                        canonical_url  = COALESCE(excluded.canonical_url, items.canonical_url),
                        discussion_url = COALESCE(excluded.discussion_url, items.discussion_url),
                        author         = COALESCE(excluded.author, items.author),
                        text           = CASE WHEN excluded.text != '' THEN excluded.text ELSE items.text END,
                        created_at     = COALESCE(items.created_at, excluded.created_at),
                        last_seen_at   = excluded.last_seen_at,
                        metrics        = json_patch(items.metrics, excluded.metrics),
                        tags           = CASE WHEN excluded.tags != '[]' THEN excluded.tags ELSE items.tags END,
                        extra          = json_patch(items.extra, excluded.extra)
                    """,
                    (
                        it.id, it.source, it.native_id, it.kind, it.channel, it.title.strip(), it.url,
                        canonical_url(it.url), it.discussion_url, it.author, it.text or "",
                        to_iso(it.created_at), ts, ts, metrics,
                        json.dumps(sorted(set(it.tags))), json.dumps(_clean(it.extra)),
                    ),
                )
                if it.metrics:
                    self.conn.execute(
                        """
                        INSERT INTO metric_snapshots (item_id, run_id, taken_at, metrics) VALUES (?,?,?,?)
                        ON CONFLICT(item_id, run_id) DO UPDATE SET metrics = json_patch(metric_snapshots.metrics, excluded.metrics)
                        """,
                        (it.id, run_id, ts, metrics),
                    )
                self._write_refs(it)
        return len(items), sum(1 for i in ids if i not in existing)

    def _write_refs(self, it: Item) -> None:
        refs: dict[str, int] = {}
        for r in refs_from_text(it.text):
            refs[r] = 0
        for r in it.extra.get("refs", []):
            refs[r] = 0
        if primary := url_ref(it.url):
            refs[primary] = 1
        for r in it.extra.get("primary_refs", []):
            refs[r] = 1
        for ref, primary_flag in refs.items():
            self.conn.execute(
                """
                INSERT INTO item_refs (item_id, ref, primary_ref) VALUES (?,?,?)
                ON CONFLICT(item_id, ref) DO UPDATE SET primary_ref = max(primary_ref, excluded.primary_ref)
                """,
                (it.id, ref, primary_flag),
            )


def _chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
