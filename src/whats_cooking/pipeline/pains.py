"""Complaint mining: find statements of real, unmet needs (no Claude involved).

Sources:
  * comment threads of well-discussed HN / Lobsters stories (fetched once, re-mined if they grow)
  * Discourse product-forum threads (first posts)
  * GitHub issues, Stack Exchange questions, Ask HN posts, social posts (the item itself)
  * company demand: HN "Who is hiring" posts, YC Requests for Startups
Each mention records how many *people* it represents: an issue with 120 thumbs-up is 121 people
with the problem, a question with score 30 is ~31.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from whats_cooking.collectors.base import strip_html
from whats_cooking.config import Settings
from whats_cooking.db import DB, now_iso
from whats_cooking.http import Fetcher
from whats_cooking.pipeline.score import source_group

log = logging.getLogger(__name__)

# (pattern, weight). A statement needs total weight >= MIN_WEIGHT.
PATTERNS: list[tuple[re.Pattern, float]] = [(re.compile(p, re.I), w) for p, w in [
    (r"\bI wish (there (was|were)|someone would)\b", 3),
    (r"\bI wish (I could|it (would|could|had|did))\b", 1.5),
    (r"\b(it'?s|is|was|are|were) (so |really |incredibly |extremely |still )?(annoying|painful|frustrating|broken|unusable|clunky|janky)\b", 2),
    (r"\b(my|our|the) (biggest|main|only|one) (gripe|complaint)\b", 2.5),
    (r"\bthere'?s no (way|tool|option|alternative|solution|good|easy|simple) (to|for|that)\b", 2.5),
    (r"\b(I|we) (spent|spend|wasted|waste|lose|lost) (hours|days|weeks|so much time)\b", 2.5),
    (r"\b(lack of|lacks|lacking) (a |any |good |proper |decent )?\w+", 1),
    (r"\b(I|we) (really |desperately )?need (a|an|better|some|proper) (tool|way|solution|alternative|option)\b", 2.5),
    (r"\b(hard|difficult|impossible|a pain) to (find|use|debug|set up|deploy|maintain|scale|integrate|migrate|test|monitor)\b", 2),
    (r"\b(vendor )?lock-?in\b", 1),
    (r"\b(gave|giving) up (on|trying)\b", 1.5),
    (r"\b(switched|moved|migrated) away from\b", 1.5),
    (r"\b(missing|lacking) piece\b", 2),
    (r"\bwhy (is there no|isn'?t there|can'?t (I|we|you)|doesn'?t (it|anyone))\b", 3),
    (r"\b(is|are) there (any|a|an) (good |decent |simple |reliable |open[- ]source )?(tool|way|library|app|service|alternative|solution|product)s?\b", 2.5),
    (r"\b(I'?d|I would|we'?d|we would) (happily |gladly )?pay\b", 3),
    # Wishes like this are common and rarely turn into paying users; a workaround (below) is a better sign.
    (r"\bsomeone (should|needs to|has to) (build|make|fix)\b", 1.5),
    (r"\bno (good|decent|easy|reliable|simple|sane|proper) (way|tool|solution|option|alternative|answer)\b", 3),
    (r"\b(nobody|no one) (has )?(solved|fixed|built)\b", 3),
    (r"\b(biggest|main|real) (problem|pain|issue|challenge|headache|bottleneck)\b", 2),
    (r"\b(pain point|painful|nightmare|headache)\b", 2),
    (r"\b(frustrat|infuriat)\w*", 1.5),
    (r"\b(we|I) (have|had|ended up) (to )?(manually|writ(e|ing) our own|build(ing)? our own|roll(ing)? our own|hack(ing)? together)\b", 3),
    (r"\b(workaround|duct[- ]tape|band-?aid)\b", 1.5),
    (r"\b(tedious|time[- ]consuming|waste of time|error[- ]prone|soul[- ]crushing)\b", 1.5),
    (r"\b(still )?(doesn'?t|does not|never) (work|scale)s?\b", 1),
    (r"\btoo (expensive|slow|complex|complicated|hard|unreliable|flaky)\b", 1.5),
    (r"\b(struggl\w+|can'?t figure out|no idea how to)\b", 1.5),
    (r"\b(unsolved|unsolvable|still broken|still no)\b", 2),
    (r"\bcan'?t find (a|any|anything)\b", 2),
    (r"\b(missing feature|feature request|please add|would love (a|to see|it if))\b", 1.5),
    (r"\b(costs? (us|me) (hours|days|weeks)|hours (every|each|per) (day|week))\b", 2),
]]
# People already working around a problem (spreadsheets, scripts, manual steps) are the strongest sign
# that it's worth solving: they're spending real effort on it today.
# Always first person: someone describing what THEY do today, not an idiom ("by hand"), a question
# ("is there a workaround?") or a criticism ("reads like copy-paste").
WORKAROUND = re.compile(
    r"\b((I|we) (have|had|ended up|resort(ed)?) to (manually|copy|export|re-?enter|write a script|maintain|hack)\w*"
    r"|(I|we) (still |now |currently )?(use|keep|maintain|built|wrote|run) (a |an |our own |my own |some )?"
    r"(spreadsheet|google sheets?|excel (sheet|file)|notion (page|database)|airtable|(bash |python |little |small )?script|"
    r"cron job|zapier|internal tool)"
    r"|(I|we) (wrote|built|rolled|hacked together|cobbled together) (our|my) own"
    r"|(I|we) (still |have to |end up )?manually (copy|enter|check|update|export|import|track|move|reconcile|merge|sync)\w*"
    r"|(our|my) (current )?workaround (is|was)|as a workaround,? (I|we))\b", re.I)
MIN_WEIGHT = 2.0
_SENT = re.compile(r"(?<=[.!?])\s+|\n+")
_FEATURE_REQUEST = re.compile(
    r"\b(feature|enhancement|request|proposal|support (for|of)|add (a |an |the )?\w+|allow\w*|option to|ability to|"
    r"would (be nice|love)|please (add|support)|rfc|idea)\b", re.I)


@dataclass
class Mention:
    item_id: str
    source: str
    community: str
    mention_type: str          # 'user' | 'company'
    author: str | None
    text: str
    context: str
    url: str | None
    created_at: str | None
    weight: float
    people: int


def complaint_sentences(text: str, max_hits: int = 3) -> list[tuple[str, str, float]]:
    """(sentence, context, weight) for sentences that read like an unmet need."""
    text = text or ""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]   # drop quoted replies
    sents = [s.strip() for s in _SENT.split("\n".join(lines)) if s.strip()]
    hits = []
    for i, s in enumerate(sents):
        # Quotes of someone else (HN flattens "> quote" into the running text) aren't a new voice.
        if not 30 <= len(s) <= 500 or "```" in s or s.startswith((">", '"', "“")):
            continue
        w = sum(weight for pat, weight in PATTERNS if pat.search(s))
        if WORKAROUND.search(s):
            w += 2.0   # enough on its own: describing a workaround is the strongest sign of real pain
        if w >= MIN_WEIGHT:
            context = " ".join(sents[max(0, i - 1): i + 2])[:600]
            hits.append((s[:400], context, w))
    hits.sort(key=lambda h: -h[2])
    return hits[:max_hits]


def harvest(settings: Settings, db: DB, fetcher: Fetcher | None = None) -> int:
    """Mine new statements into pain_mentions. Returns the number of new mentions."""
    cfg = {"lookback_days": 3, "min_comments": 15, "remine_growth": 1.5, "max_threads": 120,
           **((settings.brief.get("problems") or {}).get("harvest") or {})}
    since = (datetime.now(UTC) - timedelta(days=cfg["lookback_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    mentions: list[Mention] = []
    mentions += _from_items(db, since)

    threads = _threads_to_mine(db, since, cfg)
    if threads:
        own = fetcher is None
        fetcher = fetcher or Fetcher()
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda t: _mine_thread(fetcher, t), threads))
        finally:
            if own:
                fetcher.close()
        for t, found in zip(threads, results):
            if found is None:
                continue
            mentions += found
            with db.conn:
                db.conn.execute(
                    "INSERT OR REPLACE INTO mined (item_id, comments_at_mining, mined_at) VALUES (?,?,?)",
                    (t["id"], t["comments"], now_iso()),
                )
    return _store(db, mentions)


# ---- the items themselves -------------------------------------------------------

def _from_items(db: DB, since: str) -> list[Mention]:
    out: list[Mention] = []
    rows = db.conn.execute(
        """
        SELECT id, source, kind, channel, title, text, url, discussion_url, author, metrics, extra, tags,
               COALESCE(created_at, first_seen_at) AS ts
        FROM items WHERE first_seen_at >= ? AND kind IN ('issue', 'question', 'ask', 'thread', 'post', 'story', 'job', 'request')
        """,
        (since,),
    ).fetchall()
    for r in rows:
        m, extra = json.loads(r["metrics"]), json.loads(r["extra"])
        community = source_group(r["source"], r["kind"], extra)
        url = r["discussion_url"] or r["url"]
        if r["kind"] in ("job", "request"):
            text = (r["text"] or r["title"])
            if r["kind"] == "job":   # drop the "Company | Role | Location | Salary |" header: keep what they build
                text = re.sub(r"^.{0,300}\|\s*", "", text, flags=re.S) or text
            text = text[:600]
            people = 25 if r["kind"] == "request" else 1   # an investor's public request = strong demand
            out.append(Mention(r["id"], r["source"], community, "company", r["author"], text[:400], text,
                               url, r["ts"], 3.0, people))
            continue
        body = f"{r['title']}. {r['text'] or ''}"
        if r["kind"] == "issue":
            # The issue *is* the complaint; thumbs-up are other people with the same problem.
            # Feature requests are unmet needs; bug reports are mostly one product's defects, so they count less.
            people = 1 + int(m.get("thumbs_up") or 0)
            labels = " ".join(json.loads(r["tags"] or "[]"))
            if not _FEATURE_REQUEST.search(f"{r['title']} {labels}"):
                people = 1 + (people - 1) // 5
            # Drop the "[owner/repo] " prefix: otherwise every issue of one repo looks like the same problem.
            title = re.sub(r"^\[[^\]]+\]\s*", "", r["title"])
            out.append(Mention(r["id"], r["source"], community, "user", r["author"], title[:400],
                               body[:600], url, r["ts"], 3.0, people))
            continue
        if r["kind"] == "question":
            people = 1 + max(int(m.get("score") or 0), 0) + int(m.get("views") or 0) // 2000
            hits = complaint_sentences(body, 1)
            text, ctx, w = hits[0] if hits else (r["title"], body[:600], 2.5)
            out.append(Mention(r["id"], r["source"], community, "user", r["author"], text, ctx, url, r["ts"], w, people))
            continue
        for text, ctx, w in complaint_sentences(body, 2):
            people = 1 + (int(m.get("points") or 0) // 10 if r["kind"] == "ask" else int(m.get("likes") or 0) // 5)
            out.append(Mention(r["id"], r["source"], community, "user", r["author"], text, ctx, url, r["ts"], w, people))
    return out


# ---- comment threads ----------------------------------------------------------------

def _threads_to_mine(db: DB, since: str, cfg: dict) -> list[dict]:
    rows = db.conn.execute(
        """
        SELECT it.id, it.source, it.native_id, it.channel, it.discussion_url, it.extra,
               CAST(COALESCE(json_extract(it.metrics, '$.comments'), json_extract(it.metrics, '$.replies'), 0) AS INTEGER) AS comments,
               m.comments_at_mining
        FROM items it LEFT JOIN mined m ON m.item_id = it.id
        WHERE it.first_seen_at >= ?
          AND ((it.source IN ('hackernews', 'lobsters') AND it.kind IN ('story', 'show', 'ask')) OR it.source = 'discourse')
        """,
        (since,),
    ).fetchall()
    # Skip threads on off-topic news (politics, crime...): their complaints aren't product problems.
    off_topic = {r[0] for r in db.conn.execute(
        """
        SELECT ti.item_id FROM topic_items ti JOIN topic_scores s ON s.topic_id = ti.topic_id
        WHERE s.scored_at = (SELECT MAX(scored_at) FROM topic_scores)
          AND CAST(json_extract(s.features, '$.relevance') AS REAL) < 0.5
        """)}
    out = []
    for r in rows:
        if r["id"] in off_topic:
            continue
        threshold = cfg["min_comments"] if r["source"] != "discourse" else 3
        if r["comments"] < threshold:
            continue
        if r["comments_at_mining"] is not None and r["comments"] < r["comments_at_mining"] * cfg["remine_growth"]:
            continue
        out.append(dict(r))
    out.sort(key=lambda r: -r["comments"])
    return out[: cfg["max_threads"]]


def _mine_thread(fetcher: Fetcher, t: dict) -> list[Mention] | None:
    try:
        if t["source"] == "hackernews":
            comments = _hn_comments(fetcher, t["native_id"])
        elif t["source"] == "lobsters":
            data = fetcher.get_json(f"https://lobste.rs/s/{t['native_id']}.json")
            comments = [(c.get("commenting_user") if isinstance(c.get("commenting_user"), str)
                         else (c.get("commenting_user") or {}).get("username"),
                         strip_html(c.get("comment")), c.get("score") or 0, c.get("created_at"))
                        for c in data.get("comments", [])]
        else:
            host, tid = t["native_id"].split(":", 1)
            data = fetcher.get_json(f"https://{host}/t/{tid}.json")
            comments = [(p.get("username"), strip_html(p.get("cooked")), p.get("like_count") or 0, p.get("created_at"))
                        for p in data.get("post_stream", {}).get("posts", [])[:30]]
    except Exception as exc:
        log.debug("mining %s failed: %s", t["id"], exc)
        return None
    community = source_group(t["source"], "story", json.loads(t["extra"] or "{}"))
    out, seen = [], set()
    for author, text, score, created in comments:
        for sent, ctx, w in complaint_sentences(text, 2):
            norm = re.sub(r"\W+", " ", sent.lower()).strip()[:120]
            if norm in seen:            # the same sentence quoted back in replies isn't another person
                continue
            seen.add(norm)
            people = 1 + max(int(score or 0), 0) // 3
            out.append(Mention(t["id"], t["source"], community, "user", author, sent, ctx,
                               t["discussion_url"], created, w, people))
    return out


def _hn_comments(fetcher: Fetcher, story_id: str) -> list[tuple]:
    data = fetcher.get_json(f"https://hn.algolia.com/api/v1/items/{story_id}")
    out, stack = [], list(data.get("children", []))
    while stack:
        c = stack.pop()
        if c.get("text"):
            out.append((c.get("author"), strip_html(c.get("text")), 0, c.get("created_at")))
        stack.extend(c.get("children") or [])
    return out


def _store(db: DB, mentions: list[Mention]) -> int:
    keyed = {hashlib.sha1(f"{m.item_id}|{m.author}|{m.text}".encode()).hexdigest(): m for m in mentions}
    existing: set[str] = set()
    keys = list(keyed)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        existing.update(r[0] for r in db.conn.execute(
            f"SELECT key FROM pain_mentions WHERE key IN ({','.join('?' * len(chunk))})", chunk))
    with db.conn:
        for key, m in keyed.items():
            signal = "workaround" if m.mention_type == "user" and WORKAROUND.search(m.context or m.text) else None
            db.conn.execute(
                """
                INSERT INTO pain_mentions (key, item_id, source, community, mention_type, author, text, context, url,
                                           created_at, weight, people, first_seen_at, signal)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET people = MAX(people, excluded.people)
                """,
                (key, m.item_id, m.source, m.community, m.mention_type, m.author, m.text, m.context, m.url,
                 m.created_at, m.weight, m.people, now_iso(), signal),
            )
    return len(set(keyed) - existing)
