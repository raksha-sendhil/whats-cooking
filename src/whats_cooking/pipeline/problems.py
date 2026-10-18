"""Problems: complaints grouped into unmet needs, tracked over time, analysed once by Claude.

  assign_problems  (no Claude) embed new mentions; group user complaints into problems; YC requests
                   become problems of their own; hiring posts stay as evidence of who's working on what.
  score_problems   (no Claude) breadth of *people*, communities, persistence over days, company activity.
  write_cards      (Claude, budget-guarded) for new or fast-growing problems only: who has it, what exists,
                   why it's unsolved, possible solutions and why each hasn't been built yet.
  render_board     (no Claude) briefs/problems.md, the always-current list.
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from datetime import UTC, datetime, timedelta

import numpy as np

from whats_cooking.config import Settings
from whats_cooking.db import DB, now_iso
from whats_cooking.llm import LLMResult, call_claude
from whats_cooking.pipeline.brief import PLAIN_LANGUAGE
from whats_cooking.pipeline.group import DEFAULTS as GROUP_DEFAULTS
from whats_cooking.pipeline.group import Embedder, _Matcher, _normalize, title_tokens

log = logging.getLogger(__name__)

DEFAULTS = {
    "window_days": 60,
    "rules": [[0.72, 0.0], [0.55, 0.4]],     # join a problem: cosine (and distinctive-word overlap)
    "min_authors": 3,                        # surface problems raised by >= 3 different people
    "min_threads": 2,                        # ...in >= 2 separate threads/issues/posts (not one pile-on)...
    "min_people": 100,                       # ...or a single item representing >= 100 people
    "max_cards_per_run": 6,
    "max_request_cards_per_run": 2,
    "card_refresh_growth": 2.0,              # re-analyse when the number of people doubles
    "company_evidence_similarity": 0.45,
}

SYSTEM = (
    "You analyse problems that real people and companies are complaining about online, for a builder looking "
    "for problems worth solving. Be concrete and honest: if something isn't a real, widespread, solvable problem "
    "(a one-off bug, a personal gripe, off-topic chatter, a rant about one company with no product gap), mark "
    "is_real false. People describing a workaround they already use (spreadsheets, scripts, manual steps) are "
    "much stronger evidence than people wishing someone would build something. Never invent evidence; use the "
    "numbers and quotes given. Refer to problems only by their id "
    "(like P12) in the `problem` field, never in prose. " + PLAIN_LANGUAGE
)

SCHEMA = {
    "type": "object",
    "properties": {"cards": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "problem": {"type": "string"},
            "is_real": {"type": "boolean"},
            "title": {"type": "string", "description": "short name of the problem"},
            "summary": {"type": "string", "description": "who has it and what exactly hurts"},
            "evidence": {"type": "string", "description": "how widespread, citing the counts and communities"},
            "current_options": {"type": "string", "description": "what people use today and why it falls short"},
            "why_unsolved": {"type": "string"},
            "solutions": {"type": "array", "maxItems": 3, "items": {
                "type": "object",
                "properties": {
                    "idea": {"type": "string"},
                    "how": {"type": "string"},
                    "why_not_done_yet": {"type": "string", "description": "technical, economic, distribution or regulatory reason"},
                    "difficulty": {"type": "string", "enum": ["weekend project", "months", "hard / research"]},
                },
                "required": ["idea", "how", "why_not_done_yet", "difficulty"],
            }},
            "opportunity": {"type": "string", "description": "size, and who would pay"},
            "first_step": {"type": "string", "description": "one concrete first step for a solo builder"},
        },
        "required": ["problem", "is_real", "title", "summary", "evidence", "current_options", "why_unsolved",
                     "solutions", "opportunity", "first_step"],
    }}},
    "required": ["cards"],
}


def _cfg(settings: Settings) -> dict:
    return {**DEFAULTS, **(settings.brief.get("problems") or {})}


def eligible(p, cfg: dict) -> bool:
    """Widespread enough to show: several people across separate threads, or one huge item."""
    return (p["kind"] == "request"
            or (p["authors"] >= cfg["min_authors"] and p["threads"] >= cfg["min_threads"])
            or p["people"] >= cfg["min_people"])


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- grouping ------------------------------------------------------------------

def assign_problems(settings: Settings, db: DB, embedder=None) -> dict:
    cfg = _cfg(settings)
    conn = db.conn
    pending = conn.execute("SELECT id, text FROM pain_mentions WHERE vector IS NULL").fetchall()
    if pending:
        if embedder is None:
            model = (settings.grouping or {}).get("model", GROUP_DEFAULTS["model"])
            embedder = Embedder(model, cache_dir=str(settings.root / "data" / "models"))
        vecs = embedder.embed([r["text"] for r in pending])
        with conn:
            conn.executemany("UPDATE pain_mentions SET vector=? WHERE id=?",
                             [(v.tobytes(), r["id"]) for r, v in zip(pending, vecs)])

    since = _iso(datetime.now(UTC) - timedelta(days=cfg["window_days"]))
    rows = conn.execute(
        """
        SELECT pm.id, pm.item_id, pm.text, pm.vector, pm.problem_id, pm.mention_type, it.kind AS item_kind
        FROM pain_mentions pm LEFT JOIN items it ON it.id = pm.item_id
        WHERE pm.first_seen_at >= ? AND pm.vector IS NOT NULL
        ORDER BY pm.id
        """,
        (since,),
    ).fetchall()
    texts = [r["text"] for r in rows]
    df = Counter(t for x in texts for t in title_tokens(x))
    n = max(len(texts), 1)
    matcher = _Matcher({"discussion": cfg["rules"]}, {w: math.log(n / (1 + c)) + 1 for w, c in df.items()}, math.log(n) + 1)

    vecs, toks, pids = [], [], []           # already-assigned user mentions = the index
    new_assignments: list[tuple[int, int]] = []
    created = 0
    next_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM problems").fetchone()[0] + 1
    ts = now_iso()
    with conn:
        for r in rows:
            if r["mention_type"] == "company":
                if r["item_kind"] == "request" and r["problem_id"] is None:
                    # Each investor request is a problem in its own right.
                    title = conn.execute("SELECT title FROM items WHERE id=?", (r["item_id"],)).fetchone()
                    conn.execute(
                        "INSERT INTO problems (id, kind, title, first_seen_at, last_seen_at) VALUES (?, 'request', ?, ?, ?)",
                        (next_id, (title[0] if title else r["text"][:80]), ts, ts))
                    conn.execute("UPDATE pain_mentions SET problem_id=? WHERE id=?", (next_id, r["id"]))
                    next_id += 1
                    created += 1
                continue                    # hiring posts are evidence, not problems
            v, tk = np.frombuffer(r["vector"], dtype=np.float32), title_tokens(r["text"])
            pid = r["problem_id"]
            if pid is None:
                if vecs:
                    sims = np.stack(vecs) @ v
                    for i in np.argsort(-sims)[:20]:
                        if matcher.matches(float(sims[i]), "story", tk, "story", toks[i]):
                            pid = pids[i]
                            break
                if pid is None:
                    pid = next_id
                    next_id += 1
                    created += 1
                    conn.execute("INSERT INTO problems (id, kind, title, first_seen_at, last_seen_at) VALUES (?, 'user', ?, ?, ?)",
                                 (pid, r["text"][:200], ts, ts))
                new_assignments.append((pid, r["id"]))
            vecs.append(v)
            toks.append(tk)
            pids.append(pid)
        conn.executemany("UPDATE pain_mentions SET problem_id=? WHERE id=?", new_assignments)
    _refresh(conn, {p for p, _ in new_assignments} | set(_request_ids(conn)))
    return {"embedded": len(pending), "assigned": len(new_assignments), "new_problems": created}


def _request_ids(conn) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM problems WHERE kind='request'")]


def _refresh(conn, problem_ids: set[int]) -> None:
    """Recompute counts, centroid and representative title for the given problems."""
    with conn:
        for pid in problem_ids:
            ms = conn.execute(
                """
                SELECT item_id, author, people, community, mention_type, text, vector, signal,
                       COALESCE(created_at, first_seen_at) AS ts, first_seen_at
                FROM pain_mentions WHERE problem_id=?
                """,
                (pid,),
            ).fetchall()
            if not ms:
                continue
            by_author: dict[str, int] = {}
            for i, m in enumerate(ms):
                key = m["author"] or f"anon-{i}"
                by_author[key] = max(by_author.get(key, 0), m["people"])
            vecs = [np.frombuffer(m["vector"], dtype=np.float32) for m in ms if m["vector"]]
            centroid = _normalize(np.mean(np.stack(vecs), axis=0)) if vecs else None
            days = {(m["ts"] or m["first_seen_at"])[:10] for m in ms}
            kind = conn.execute("SELECT kind FROM problems WHERE id=?", (pid,)).fetchone()[0]
            title = None
            if kind == "user" and centroid is not None:
                best = max((m for m in ms if m["vector"]), key=lambda m: float(np.frombuffer(m["vector"], dtype=np.float32) @ centroid))
                title = best["text"][:200]
            conn.execute(
                """
                UPDATE problems SET title=COALESCE(?, title), centroid=?, mentions=?, people=?, authors=?, threads=?,
                       workarounds=?, communities=?, days_active=?, first_seen_at=?, last_seen_at=? WHERE id=?
                """,
                (title, centroid.astype(np.float32).tobytes() if centroid is not None else None, len(ms),
                 sum(by_author.values()), len(by_author), len({m["item_id"] for m in ms}),
                 len({m["author"] or f"anon-{i}" for i, m in enumerate(ms) if m["signal"] == "workaround"}),
                 json.dumps(sorted({m["community"] for m in ms})),
                 len(days), min(m["first_seen_at"] for m in ms), max(m["first_seen_at"] for m in ms), pid),
            )


# ---- scoring -------------------------------------------------------------------------

def company_evidence(db: DB, centroid: np.ndarray, threshold: float, limit: int = 6) -> list[tuple[float, str, str]]:
    """Hiring posts closest to a problem: (similarity, company, snippet)."""
    rows = db.conn.execute(
        "SELECT pm.text, pm.vector, it.title FROM pain_mentions pm JOIN items it ON it.id = pm.item_id "
        "WHERE pm.mention_type='company' AND it.kind='job' AND pm.vector IS NOT NULL").fetchall()
    if not rows:
        return []
    sims = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows]) @ centroid
    out = [(float(sims[i]), rows[i]["title"], rows[i]["text"]) for i in np.argsort(-sims)[:limit] if sims[i] >= threshold]
    return out


def score_problems(settings: Settings, db: DB, now: datetime | None = None) -> list[dict]:
    cfg = _cfg(settings)
    now = now or datetime.now(UTC)
    out = []
    with db.conn:
        for p in db.conn.execute("SELECT * FROM problems WHERE status='open'").fetchall():
            ok = eligible(p, cfg)
            centroid = np.frombuffer(p["centroid"], dtype=np.float32) if p["centroid"] else None
            companies = len(company_evidence(db, centroid, cfg["company_evidence_similarity"], 10)) if (centroid is not None and ok) else 0
            age_days = (now - datetime.fromisoformat(p["last_seen_at"].replace("Z", "+00:00"))).total_seconds() / 86400
            recency = 1.0 if age_days <= 3 else 0.5 ** ((age_days - 3) / 7)
            comp = {
                "people": min(math.log1p(p["people"]) / math.log1p(300), 1.0),
                "breadth": min(len(json.loads(p["communities"])) - 1, 3) / 3,
                "persistence": min(p["days_active"] - 1, 6) / 6,
                "companies": min(companies, 10) / 10,
                # People already hacking around it (spreadsheets, scripts, manual steps) = real, paid-for pain.
                "workarounds": min(p["workarounds"] / 3, 1.0),
            }
            score = (0.33 * comp["people"] + 0.2 * comp["breadth"] + 0.17 * comp["persistence"]
                     + 0.12 * comp["companies"] + 0.18 * comp["workarounds"]) * recency
            if not ok:
                score *= 0.1
            db.conn.execute("UPDATE problems SET score=?, company_mentions=? WHERE id=?", (round(score, 5), companies, p["id"]))
            out.append({"id": p["id"], "score": score, "eligible": ok, **comp})
    out.sort(key=lambda x: -x["score"])
    return out


# ---- Claude cards -------------------------------------------------------------------------

def cards_due(settings: Settings, db: DB) -> list[int]:
    cfg = _cfg(settings)
    rows = db.conn.execute(
        "SELECT id, kind, people, card, card_people, authors, threads FROM problems WHERE status='open' ORDER BY score DESC"
    ).fetchall()
    user, requests = [], []
    for r in rows:
        if not eligible(r, cfg):
            continue
        due = r["card"] is None or r["people"] >= (r["card_people"] or 0) * cfg["card_refresh_growth"]
        if not due:
            continue
        (requests if r["kind"] == "request" else user).append(r["id"])
    return user[: cfg["max_cards_per_run"]] + requests[: cfg["max_request_cards_per_run"]]


def _problem_block(settings: Settings, db: DB, pid: int) -> str:
    cfg = _cfg(settings)
    p = db.conn.execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone()
    lines = [f"### P{pid} | {'investor request (YC)' if p['kind'] == 'request' else 'user complaints'} | "
             f"{p['people']} people ({p['authors']} distinct authors), {p['mentions']} mentions, "
             f"communities: {', '.join(json.loads(p['communities']))}, active on {p['days_active']} day(s)"
             + (f", {p['workarounds']} describe a workaround they already use" if p["workarounds"] else "")]
    ms = db.conn.execute(
        """SELECT source, author, people, text, context, signal FROM pain_mentions WHERE problem_id=?
           ORDER BY signal = 'workaround' DESC, people DESC LIMIT 40""",
        (pid,)).fetchall()
    seen_authors, shown = set(), 0
    lines.append("What people said:")
    for m in ms:
        if m["author"] and m["author"] in seen_authors:
            continue
        seen_authors.add(m["author"])
        quote = (m["context"] if p["kind"] == "request" else m["text"])[: 1200 if p["kind"] == "request" else 300]
        tag = ", already working around it" if m["signal"] == "workaround" else ""
        lines.append(f'- [{m["source"]}, ~{m["people"]} people{tag}] "{quote}"')
        shown += 1
        if shown >= 8:
            break
    if p["centroid"]:
        centroid = np.frombuffer(p["centroid"], dtype=np.float32)
        companies = company_evidence(db, centroid, cfg["company_evidence_similarity"])
        if companies:
            lines.append("Companies hiring in this area (HN Who is hiring):")
            lines += [f"- {c[1][:60]}: {c[2][:180]}" for c in companies]
        topics = _related_topics(db, centroid)
        if topics:
            lines.append("Recent news/releases that might be relevant:")
            lines += [f"- {t}" for t in topics]
    return "\n".join(lines)


def _related_topics(db: DB, centroid: np.ndarray, limit: int = 3, threshold: float = 0.4) -> list[str]:
    since = _iso(datetime.now(UTC) - timedelta(days=14))
    rows = db.conn.execute(
        "SELECT title, centroid FROM topics WHERE merged_into IS NULL AND centroid IS NOT NULL AND item_count >= 2 AND last_seen_at >= ?",
        (since,)).fetchall()
    if not rows:
        return []
    sims = np.stack([np.frombuffer(r["centroid"], dtype=np.float32) for r in rows]) @ centroid
    return [rows[i]["title"][:150] for i in np.argsort(-sims)[:limit] if sims[i] >= threshold]


def cards_prompt(settings: Settings, db: DB, due: list[int]) -> str:
    profile = settings.profile.strip()
    return (f"# Reader profile\n{profile}\n\n# Task\nFor each problem below, write a card. Judge whether it's a real, "
              "widespread problem worth solving (is_real). For real ones: who has it and what hurts, the evidence, what "
              "exists today and why it falls short, why it's still unsolved, 2-3 possible solutions (each with how it "
              "would work, why nobody has done it yet, and difficulty), the opportunity (size, who pays), and one first "
              "step for a solo builder. Use the related news when a new capability could enable a solution.\n\n# Problems\n"
              + "\n\n".join(_problem_block(settings, db, pid) for pid in due))


def write_cards(settings: Settings, db: DB, runner=None) -> tuple[list[int], LLMResult | None]:
    """Analyse due problems with one Claude call. Returns (ids that got a card, llm result)."""
    due = cards_due(settings, db)
    if not due:
        return [], None
    r = call_claude(settings, db, "problems", cards_prompt(settings, db, due), SYSTEM, schema=SCHEMA, runner=runner)
    if not r.ok or not r.data:
        return [], r
    done = []
    with db.conn:
        for card in r.data.get("cards", []):
            try:
                pid = int(str(card.get("problem", "")).lstrip("Pp"))
            except ValueError:
                continue
            if pid not in due:
                continue
            people = db.conn.execute("SELECT people FROM problems WHERE id=?", (pid,)).fetchone()[0]
            status = "open" if card.get("is_real", True) else "dismissed"
            db.conn.execute("UPDATE problems SET card=?, card_at=?, card_people=?, status=? WHERE id=?",
                            (json.dumps(card), now_iso(), people, status, pid))
            if status == "open":
                done.append(pid)
    return done, r


# ---- rendering ---------------------------------------------------------------------------

def render_card(p, card: dict, heading: str = "###") -> str:
    stats = [f"{p['people']} people", f"{p['authors']} distinct authors in {p['threads']} threads",
             ", ".join(json.loads(p["communities"]))]
    if p["workarounds"]:
        stats.append(f"{p['workarounds']} already working around it")
    stats.append(f"active {p['days_active']} day(s)")
    if p["company_mentions"]:
        stats.append(f"{p['company_mentions']} hiring companies nearby")
    if p["kind"] != "user":
        stats.append("YC Request for Startups")
    out = [f"{heading} {card.get('title') or p['title']}\n",
           f"_{' · '.join(stats)}_\n",
           f"**The problem:** {card['summary']}\n",
           f"**Evidence:** {card['evidence']}\n",
           f"**What exists today:** {card['current_options']}\n",
           f"**Why it's unsolved:** {card['why_unsolved']}\n",
           "**Possible solutions:**\n"]
    for s in card.get("solutions", []):
        out.append(f"- **{s['idea']}** ({s['difficulty']}): {s['how']} *Why not done yet:* {s['why_not_done_yet']}")
    out.append(f"\n**Opportunity:** {card['opportunity']}\n")
    out.append(f"**First step:** {card['first_step']}\n")
    return "\n".join(out)


def render_board(settings: Settings, db: DB) -> str:
    rows = db.conn.execute(
        "SELECT * FROM problems WHERE status='open' AND card IS NOT NULL ORDER BY kind='request', score DESC").fetchall()
    pending = db.conn.execute(
        """
        SELECT * FROM problems WHERE status='open' AND card IS NULL AND kind='user' AND (authors >= 2 OR people >= 20)
        ORDER BY authors DESC, people DESC LIMIT 15
        """).fetchall()
    out = ["# Unsolved problems board\n",
           "_Problems people keep complaining about, ranked by how many different people raise them, across how many "
           "threads and communities, for how long. A problem gets a full analysis once 3+ people raise it in 2+ separate "
           "threads (or one item represents 100+ people)._\n"]
    user = [r for r in rows if r["kind"] == "user"]
    if user:
        out.append("| # | Problem | People | Communities | Days |\n| --- | --- | --- | --- | --- |")
        for n, r in enumerate(user, 1):
            card = json.loads(r["card"])
            out.append(f"| {n} | {card.get('title', r['title'])[:80]} | {r['people']} | {len(json.loads(r['communities']))} | {r['days_active']} |")
        out.append("")
        for r in user:
            out.append(render_card(r, json.loads(r["card"]), "##"))
    if not user:
        out.append("_No problem has enough evidence for a full analysis yet. Evidence builds up with every collection run._\n")
    if pending:
        out.append("## Emerging (not analysed yet)\n")
        out += [f"- {r['title'][:160]} ({r['people']} people, {r['authors']} authors, {r['threads']} threads)" for r in pending]
        out.append("")
    requests = [r for r in rows if r["kind"] == "request"]
    if requests:
        out.append("# Company demand: YC Requests for Startups\n")
        for r in requests:
            out.append(render_card(r, json.loads(r["card"]), "##"))
    return "\n".join(out).rstrip() + "\n"


def write_board(settings: Settings, db: DB) -> None:
    path = settings.briefs_dir / "problems.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_board(settings, db), encoding="utf-8")


def open_problem_titles(db: DB, limit: int = 10) -> list[str]:
    rows = db.conn.execute(
        "SELECT title, card, people FROM problems WHERE status='open' AND kind='user' AND card IS NOT NULL ORDER BY score DESC LIMIT ?",
        (limit,)).fetchall()
    return [f"{json.loads(r['card']).get('title', r['title'])} ({r['people']} people)" for r in rows]
