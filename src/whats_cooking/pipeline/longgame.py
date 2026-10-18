"""The long game: big, long-running challenges (config/challenges.yaml) and what moves them.

  match_topics    (every run, no Claude) new topics that mention a challenge's keywords AND are close to its
                  description in meaning are kept as candidates.
  refresh_trends  (no Claude) how often each challenge came up on Hacker News per month, from HN's free
                  full-history search; filled in gradually within a time budget, past months fetched once.
  judge           (Claude, only with an on-demand brief) the best few candidates per challenge, more for your
                  focus challenges: real progress, a small step, or not related, in plain words.
"""

from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import numpy as np

from whats_cooking.config import Settings
from whats_cooking.db import DB, now_iso
from whats_cooking.http import Fetcher
from whats_cooking.llm import LLMResult, call_claude
from whats_cooking.pipeline.brief import PLAIN_LANGUAGE, topic_sources
from whats_cooking.pipeline.score import TopicScore

log = logging.getLogger(__name__)

DEFAULTS = {
    "match_days": 7,              # only fresh topics are candidates
    "min_similarity": 0.20,       # meaning-closeness to the challenge (keywords are required too)
    "judge_per_challenge": 2,
    "judge_per_focus": 4,
    "max_judge": 16,              # hard cap on candidates sent to Claude per brief
    "trend_months": 24,
    "trend_time_budget": 300,     # seconds per run spent filling in history (only until it's complete)
}

SYSTEM = (
    "You track long-running problems in technology and judge whether new releases or research actually move "
    "them forward. Be strict: most news is 'not related' or a 'small step'. 'Real progress' means a change "
    "people could use or build on soon. Refer to candidates only by id (like C4) in the `candidate` field. "
    + PLAIN_LANGUAGE
)

SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "candidate": {"type": "string"},
            "verdict": {"type": "string", "enum": ["real progress", "small step", "not related"]},
            "headline": {"type": "string", "description": "plain, specific"},
            "what_changed": {"type": "string"},
            "why_it_helps": {"type": "string", "description": "how this moves the challenge, and by how much"},
            "for_you": {"type": "string", "description": "one concrete thing the reader could try or build"},
        },
        "required": ["candidate", "verdict", "headline", "what_changed", "why_it_helps", "for_you"],
    }}},
    "required": ["verdicts"],
}


def _cfg(settings: Settings) -> dict:
    return {**DEFAULTS, **(settings.brief.get("longgame") or {})}


def keyword_pattern(keywords: list[str]) -> re.Pattern | None:
    words = [k.strip() for k in keywords or [] if k and k.strip()]
    if not words:
        return None
    return re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + ")", re.I)


def challenge_text(c: dict) -> str:
    return f"{c['title']}. {c.get('summary', '')} {c.get('progress', '')}".strip()


def _embedder(settings: Settings, embedder):
    if embedder is not None:
        return embedder
    from whats_cooking.pipeline.group import DEFAULTS as GDEF, Embedder
    model = (settings.grouping or {}).get("model", GDEF["model"])
    return Embedder(model, cache_dir=str(settings.root / "data" / "models"))


# ---- matching ------------------------------------------------------------------------

def match_topics(settings: Settings, db: DB, scores: list[TopicScore], embedder=None) -> int:
    cfg = _cfg(settings)
    challenges = settings.challenges
    if not challenges or not scores:
        return 0
    max_age = cfg["match_days"] * 24
    fresh = [s for s in scores if (s.features.get("age_hours") or 0) <= max_age][:500]
    if not fresh:
        return 0
    vecs = _embedder(settings, embedder).embed([challenge_text(c) for c in challenges])
    patterns = [keyword_pattern(c.get("keywords", [])) for c in challenges]
    ts, n = now_iso(), 0
    with db.conn:
        for s in fresh:
            row = db.conn.execute("SELECT title, centroid FROM topics WHERE id=?", (s.topic_id,)).fetchone()
            if not row or not row["centroid"]:
                continue
            texts = " ".join(r[0] for r in db.conn.execute(
                "SELECT it.title || ' ' || substr(it.text, 1, 300) FROM topic_items ti JOIN items it ON it.id = ti.item_id "
                "WHERE ti.topic_id=? LIMIT 12", (s.topic_id,)))
            centroid = np.frombuffer(row["centroid"], dtype=np.float32)
            for c, vec, pat in zip(challenges, vecs, patterns):
                hit = pat.search(f"{row['title']} {texts}") if pat else None
                if not hit:
                    continue
                cos = float(vec @ centroid)
                if cos < cfg["min_similarity"]:
                    continue
                db.conn.execute(
                    """
                    INSERT INTO challenge_matches (challenge_id, topic_id, score, similarity, keyword, title, sources, matched_at)
                    VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(challenge_id, topic_id) DO UPDATE SET score=excluded.score, similarity=excluded.similarity,
                        title=excluded.title, sources=excluded.sources
                    """,
                    (c["id"], s.topic_id, round(s.score * (0.5 + cos), 5), round(cos, 3), hit.group(1).lower(),
                     row["title"], json.dumps(topic_sources(db, s.topic_id, 4)), ts),
                )
                n += 1
    return n


# ---- history -----------------------------------------------------------------------------

def months_back(n: int, today: date | None = None) -> list[str]:
    today = today or datetime.now(UTC).date()
    y, m, out = today.year, today.month, []
    for _ in range(n + 1):                         # n full months + the current one
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def _month_bounds(period: str) -> tuple[int, int]:
    y, m = map(int, period.split("-"))
    start = datetime(y, m, 1, tzinfo=UTC)
    end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=UTC)
    return int(start.timestamp()), int(end.timestamp())


def refresh_trends(settings: Settings, db: DB, fetcher: Fetcher | None = None, budget_s: float | None = None) -> int:
    """Fill in missing months (and refresh the current one once a day). Returns requests made."""
    cfg = _cfg(settings)
    budget = cfg["trend_time_budget"] if budget_s is None else budget_s
    periods = months_back(cfg["trend_months"])
    current = periods[-1]
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    have = {(r[0], r[1], r[2]): r[3] for r in db.conn.execute(
        "SELECT challenge_id, query, period, fetched_at FROM challenge_trends")}
    # BASELINE = all HN stories that month, so trends are shares of attention, not raw volume.
    queries = [(BASELINE, "")] + [(c["id"], q) for c in settings.challenges for q in c.get("history", [])]
    # Newest months first, so a partly filled history still shows the recent trend.
    todo = [(cid, q, p) for p in reversed(periods) for cid, q in queries
            if (cid, q, p) not in have or (p == current and not have[(cid, q, p)].startswith(today))]
    if not todo:
        return 0
    own = fetcher is None
    fetcher = fetcher or Fetcher()
    started, done = time.monotonic(), 0

    def fetch(task):
        # Count in 7-day windows: HN search's whole-month counts are sometimes estimates (one month reported
        # 366k stories instead of ~31k); week-sized windows come back exact.
        cid, q, p = task
        a, b = _month_bounds(p)
        total = 0
        for start in range(a, b, 7 * 86400):
            data = fetcher.get_json("https://hn.algolia.com/api/v1/search_by_date", params={
                "query": q, "tags": "story", "hitsPerPage": 0,
                "numericFilters": f"created_at_i>={start},created_at_i<{min(start + 7 * 86400, b)}"})
            total += int(data.get("nbHits") or 0)
        return task, total

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for i in range(0, len(todo), 16):          # in chunks, so the time budget is respected
                if time.monotonic() - started > budget:
                    log.info("trend history: time budget used, %d of %d left for next run", len(todo) - i, len(todo))
                    break
                results = []
                for fut in [pool.submit(fetch, t) for t in todo[i:i + 16]]:
                    try:
                        results.append(fut.result())
                    except Exception as exc:
                        log.debug("trend fetch failed: %s", exc)
                with db.conn:
                    db.conn.executemany(
                        "INSERT OR REPLACE INTO challenge_trends (challenge_id, query, period, count, fetched_at) VALUES (?,?,?,?,?)",
                        [(cid, q, p, n, now_iso()) for (cid, q, p), n in results])
                done += len(results)
    finally:
        if own:
            fetcher.close()
    return done


BASELINE = "_all_hn_stories"


def trend_series(db: DB, challenge_id: str, periods: list[str]) -> list[dict]:
    """Per month: raw story count, and share = stories per 10,000 HN stories that month."""
    sums = lambda cid: dict(db.conn.execute(
        "SELECT period, SUM(count) FROM challenge_trends WHERE challenge_id=? GROUP BY period", (cid,)).fetchall())
    counts, totals = sums(challenge_id), sums(BASELINE)
    return [{"period": p, "count": counts.get(p),
             "share": round(10000 * counts[p] / totals[p], 1) if counts.get(p) is not None and totals.get(p) else None}
            for p in periods]


# ---- judging (Claude) --------------------------------------------------------------------

def candidates_to_judge(settings: Settings, db: DB) -> list[tuple[dict, dict]]:
    cfg = _cfg(settings)
    since = (datetime.now(UTC) - timedelta(days=cfg["match_days"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    focus, others = [], []
    for c in settings.challenges:
        n = cfg["judge_per_focus"] if c.get("focus") else cfg["judge_per_challenge"]
        rows = db.conn.execute(
            """SELECT * FROM challenge_matches WHERE challenge_id=? AND verdict IS NULL AND matched_at >= ?
               ORDER BY score DESC LIMIT ?""", (c["id"], since, n)).fetchall()
        (focus if c.get("focus") else others).extend((c, dict(r)) for r in rows)
    # Your focus challenges first; the rest compete on strength of evidence, not on their order in the file.
    others.sort(key=lambda cm: -cm[1]["score"])
    return (focus + others)[: cfg["max_judge"]]


def _candidate_block(db: DB, n: int, c: dict, m: dict) -> str:
    items = db.conn.execute(
        "SELECT it.title, it.text FROM topic_items ti JOIN items it ON it.id = ti.item_id WHERE ti.topic_id=? LIMIT 4",
        (m["topic_id"],)).fetchall()
    excerpt = next((r["text"][:400] for r in items if r["text"] and len(r["text"]) > 80), "")
    others = "; ".join(r["title"][:100] for r in items if r["title"] != m["title"])
    return "\n".join(filter(None, [
        f"C{n} | for challenge: {c['title']}",
        f"Topic: {m['title']}",
        f"Also reported as: {others}" if others else "",
        f"Excerpt: {excerpt}" if excerpt else "",
    ]))


def judge(settings: Settings, db: DB, runner=None) -> tuple[list[tuple[str, int]], LLMResult | None]:
    """Returns ((challenge_id, topic_id) judged as progress, llm result)."""
    cands = candidates_to_judge(settings, db)
    if not cands:
        return [], None
    challenges = {c["id"]: c for c, _ in cands}
    intro = "\n\n".join(f"## {c['title']}{' (one of the reader focus areas)' if c.get('focus') else ''}\n"
                        f"{c.get('summary', '').strip()}\nReal progress would look like: {c.get('progress', '').strip()}"
                        for c in challenges.values())
    prompt = (f"# Reader profile\n{settings.profile.strip()}\n\n# Challenges\n{intro}\n\n"
              "# Task\nFor each candidate below, decide if it moves its challenge forward: 'real progress', "
              "'small step' or 'not related'. For the first two, write a plain headline, what changed, why it "
              "helps (and by how much), and one thing the reader could try or build. Keep each field short.\n\n"
              "# Candidates\n" + "\n\n".join(_candidate_block(db, i, c, m) for i, (c, m) in enumerate(cands)))
    r = call_claude(settings, db, "longgame", prompt, SYSTEM, schema=SCHEMA, runner=runner)
    if not r.ok or not r.data:
        return [], r
    progress, ts = [], now_iso()
    with db.conn:
        for v in r.data.get("verdicts", []):
            try:
                c, m = cands[int(str(v.get("candidate", "")).lstrip("Cc"))]
            except (ValueError, IndexError):
                continue
            db.conn.execute(
                "UPDATE challenge_matches SET verdict=?, judgement=?, judged_at=? WHERE challenge_id=? AND topic_id=?",
                (v.get("verdict"), json.dumps(v), ts, c["id"], m["topic_id"]))
            if v.get("verdict") != "not related":
                progress.append((c["id"], m["topic_id"]))
    return progress, r


# ---- for the reader ----------------------------------------------------------------------

def longgame_view(settings: Settings, db: DB) -> list[dict]:
    cfg = _cfg(settings)
    periods = months_back(cfg["trend_months"])
    problems = db.conn.execute(
        "SELECT title, card, people FROM problems WHERE status='open' AND kind='user'").fetchall()
    out = []
    for c in settings.challenges:
        series = trend_series(db, c["id"], periods)
        shares = [p["share"] for p in series[:-1] if p["share"] is not None]   # full months only
        change = None
        if len(shares) >= 12 and sum(shares[-12:-6]) > 0:
            change = round(100 * (sum(shares[-6:]) - sum(shares[-12:-6])) / sum(shares[-12:-6]))
        entries = [{**json.loads(r["judgement"]), "title": r["title"], "sources": json.loads(r["sources"]),
                    "judged_at": r["judged_at"]}
                   for r in db.conn.execute(
                       """SELECT * FROM challenge_matches WHERE challenge_id=? AND verdict IN ('real progress', 'small step')
                          ORDER BY judged_at DESC LIMIT 8""", (c["id"],))]
        watching = [{"title": r["title"], "sources": json.loads(r["sources"])[:2]}
                    for r in db.conn.execute(
                        """SELECT * FROM challenge_matches WHERE challenge_id=? AND verdict IS NULL
                           ORDER BY score DESC LIMIT 4""", (c["id"],))]
        pat = keyword_pattern(c.get("keywords", []))
        related = []
        for p in problems:
            text = p["title"] + " " + (p["card"] or "")
            if pat and pat.search(text):
                related.append({"title": (json.loads(p["card"]).get("title") if p["card"] else p["title"]),
                                "people": p["people"]})
        related.sort(key=lambda x: -x["people"])
        out.append({"id": c["id"], "title": c["title"], "focus": bool(c.get("focus")),
                    "summary": " ".join(c.get("summary", "").split()), "progress": " ".join(c.get("progress", "").split()),
                    "trend": series, "change_pct": change, "entries": entries, "watching": watching,
                    "complaints": {"people": sum(x["people"] for x in related), "top": related[:3]}})
    out.sort(key=lambda x: (not x["focus"]))
    return out
