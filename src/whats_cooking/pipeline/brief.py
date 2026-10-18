"""The daily brief: top topics -> compact context -> one Claude call -> Markdown.

Guarantees:
  * at most `max_topics` topics are ever sent to Claude (hard cap), and the prompt is trimmed
    to `max_prompt_chars`;
  * every call goes through the budget guard;
  * if Claude is skipped, rate-limited or fails, a signals-only brief is still written, and a
    limited/failed brief is retried on the next run (up to `max_attempts_per_day`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from whats_cooking.config import Settings
from whats_cooking.db import DB, now_iso
from whats_cooking.llm import LLMResult, call_claude
from whats_cooking.pipeline.enrich import enrich_topics
from whats_cooking.pipeline.score import TopicScore, engagement_value, latest_scores

log = logging.getLogger(__name__)

DEFAULTS = {
    "timezone": "Asia/Kolkata",
    "max_topics": 40,
    "top_stories": 10,
    "max_prompt_chars": 90000,
    "per_topic_chars": 2200,
    "enrich_top": 25,
    "repeat_cooldown_days": 3,
    "repeat_if_new_items": 3,
    "max_attempts_per_day": 3,
}

PLAIN_LANGUAGE = (
    "How to write: the reader builds software but does not follow every niche, and reads this on a phone. "
    "Use plain, everyday words and short sentences (about 20 words at most), one idea per sentence. "
    "The first time you name a company, product, model, paper or technique the reader may not know, say in a "
    "few words what it is (for example: 'vLLM, a popular tool for running LLMs on servers'). Don't explain "
    "things every developer knows (GitHub, Python, ChatGPT, a GPU). Never assume the reader saw earlier news. "
    "No stacked jargon, no compressed noun phrases, no hype words, no em-dash asides; if a phrase only makes "
    "sense with context the reader lacks, rewrite it. Say the concrete thing: numbers, names, what changed. "
    "Keep every field short: 1-3 sentences."
)

SYSTEM = (
    "You are the editor of a private daily tech brief for one reader. You are given topics already ranked by "
    "objective signals (spread across communities, engagement, growth, expert coverage). Your job is judgment: "
    "what actually matters, why, and what the reader could do with it. Prefer facts present in the material; "
    "when you infer, say so. Each topic has an id like T123: put it only in the `topic` fields, never in any "
    "prose (the reader never sees ids). " + PLAIN_LANGUAGE
)

SCHEMA = {
    "type": "object",
    "properties": {
        "themes": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "top_stories": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "topic id, e.g. T123"},
                "headline": {"type": "string", "description": "plain and specific; no puns"},
                "context": {"type": "string", "description": "background a newcomer needs to follow the story "
                            "(who/what the named things are), one or two sentences; empty if none is needed"},
                "what": {"type": "string"},
                "why_it_matters": {"type": "string"},
                "so_what": {"type": "string", "description": "what the reader could build, try, learn or watch"},
                "skeptic": {"type": "string", "description": "strongest critique or reason it may not matter"},
                "signal": {"type": "string", "enum": ["strong", "moderate", "early"]},
            },
            "required": ["topic", "headline", "context", "what", "why_it_matters", "so_what", "skeptic", "signal"],
        }},
        "quick_hits": {"type": "array", "items": {
            "type": "object",
            "properties": {"topic": {"type": "string"}, "line": {"type": "string"}},
            "required": ["topic", "line"],
        }},
        "study": {"type": "array", "maxItems": 3, "items": {
            "type": "object",
            "properties": {"concept": {"type": "string"}, "why": {"type": "string"}, "start_with": {"type": "string"}},
            "required": ["concept", "why", "start_with"],
        }},
    },
    "required": ["themes", "top_stories", "quick_hits", "study"],
}


@dataclass
class BriefResult:
    day: str
    status: str            # 'done' | 'fallback' | 'pending' | 'exists'
    path: Path | None
    topics: int
    llm: LLMResult | None = None
    note: str = ""


def local_day(settings: Settings, now: datetime | None = None) -> str:
    tz = ZoneInfo({**DEFAULTS, **settings.brief}["timezone"])
    return (now or datetime.now(UTC)).astimezone(tz).date().isoformat()


def select_topics(db: DB, cfg: dict, day: str) -> list[TopicScore]:
    """Best-scoring topics, skipping ones featured recently unless they've grown since."""
    since = (datetime.fromisoformat(day) - timedelta(days=cfg["repeat_cooldown_days"])).date().isoformat()
    featured = {r[0]: r[1] for r in db.conn.execute(
        "SELECT topic_id, MAX(item_count) FROM brief_topics WHERE day >= ? AND day < ? GROUP BY topic_id", (since, day))}
    picked = []
    for s in latest_scores(db):
        if s.topic_id in featured and s.features.get("items", 0) - featured[s.topic_id] < cfg["repeat_if_new_items"]:
            continue
        picked.append(s)
        if len(picked) >= cfg["max_topics"]:   # HARD CAP
            break
    return picked


def topic_block(db: DB, s: TopicScore, char_budget: int) -> str:
    f = s.features
    head = (f"### T{s.topic_id} | score {s.score:.2f} | {f.get('type')} | {f.get('items')} items, "
            f"{f.get('comments')} comments | communities: {', '.join(f.get('groups', []))} | "
            f"newest {f.get('age_hours')}h ago")
    rows = db.conn.execute(
        """
        SELECT it.id, it.source, it.kind, it.channel, it.title, it.url, it.discussion_url, it.text, it.metrics,
               en.comments, en.readme
        FROM topic_items ti JOIN items it ON it.id = ti.item_id
        LEFT JOIN enrichments en ON en.item_id = it.id
        WHERE ti.topic_id = ?
        """,
        (s.topic_id,),
    ).fetchall()
    rows = sorted(rows, key=lambda r: -(engagement_value(r["source"], r["kind"], json.loads(r["metrics"]), None) or 0))
    lines = [head, "Items:"]
    for r in rows[:8]:
        m = json.loads(r["metrics"])
        stats = ", ".join(f"{k} {v}" for k, v in m.items() if k in (
            "points", "score", "comments", "stars", "stars_today", "likes", "upvotes", "votes", "thumbs_up",
            "reactions", "replies", "day_rank", "trending_score"))
        lines.append(f"- [{r['source']}/{r['channel']}] {r['title'][:160]}" + (f" ({stats})" if stats else ""))
    # Prose first: artifact "text" is often just tags or a one-line description.
    prose = [r for r in rows if r["kind"] not in ("model", "space", "dataset", "repo")]
    excerpt = next((r["text"] for r in prose + rows if r["text"] and len(r["text"]) > 80), "")
    if excerpt:
        lines.append(f"Excerpt: {excerpt[:500]}")
    comments = [c for r in rows if r["comments"] for c in json.loads(r["comments"])][:5]
    if comments:
        lines.append("Top comments:")
        lines += [f'- "{c}"' for c in comments]
    readme = next((r["readme"] for r in rows if r["readme"]), "")
    if readme:
        lines.append(f"README: {readme[:600]}")
    block = "\n".join(lines)
    return block if len(block) <= char_budget else block[: char_budget - 1] + "…"


def build_prompt(settings: Settings, db: DB, topics: list[TopicScore], cfg: dict) -> str:
    from whats_cooking.pipeline.problems import open_problem_titles

    profile = settings.profile.strip() or "(no profile provided)"
    problems = open_problem_titles(db)
    problems_note = ""
    if problems:
        problems_note = ("\n# Open problems people keep complaining about (tracked separately)\n"
                         + "\n".join(f"- {p}" for p in problems)
                         + "\nWhen a story is a new capability that could help solve one of these, say so in its so_what.\n")
    focus = [c["title"] for c in settings.challenges if c.get("focus")]
    if focus:
        problems_note += ("\n# The reader's long-term focus areas\n" + "\n".join(f"- {t}" for t in focus)
                          + "\nWhen a story matters for one of these, say how in its so_what.\n")
    intro = f"""# Reader profile
{profile}

# Task
Below are today's {len(topics)} highest-signal topics (already de-duplicated and ranked by code).
Write the brief:
- top_stories: the {cfg['top_stories']} that matter most for this reader. Re-rank by real importance and fit
  with the profile, not just the given score. For each: a plain, specific headline; context (only the background
  a newcomer needs, or empty); what happened (concrete, 1-3 sentences); why it matters (what changes because of
  it, not the announcement); so_what (one specific thing the reader could build, try, learn or watch);
  skeptic (the strongest reason it may not matter, using the comments when available); signal = strong /
  moderate / early.
- quick_hits: one plain sentence each for other topics still worth knowing (up to 12). Each must make sense on
  its own. Skip noise and off-topic news.
- themes: up to 3 patterns across today's topics, one plain sentence each.
- study: up to 3 concepts worth learning now because of today's news: what it is in one sentence, why now,
  and a concrete first resource or step.
{problems_note}
# Topics
"""
    budget = cfg["max_prompt_chars"] - len(intro)
    blocks = []
    for s in topics:
        b = topic_block(db, s, cfg["per_topic_chars"])
        if budget - len(b) < 0:
            log.info("prompt budget reached after %d topics", len(blocks))
            break
        blocks.append(b)
        budget -= len(b) + 2
    return intro + "\n\n".join(blocks)


def progress_entries(settings: Settings, db: DB, pairs: list[tuple[str, int]]) -> list[dict]:
    """Long-game progress judged in this run, ready to show: challenge + Claude's entry + sources."""
    titles = {c["id"]: c["title"] for c in settings.challenges}
    focus = {c["id"] for c in settings.challenges if c.get("focus")}
    out = []
    for cid, tid in pairs:
        r = db.conn.execute("SELECT judgement, title, sources FROM challenge_matches WHERE challenge_id=? AND topic_id=?",
                            (cid, tid)).fetchone()
        if r and r["judgement"]:
            out.append({**json.loads(r["judgement"]), "challenge_id": cid, "challenge": titles.get(cid, cid),
                        "focus": cid in focus, "title": r["title"], "sources": json.loads(r["sources"])})
    out.sort(key=lambda e: (not e["focus"], e.get("verdict") != "real progress"))
    return out


def run_brief(settings: Settings, db: DB, force: bool = False, dry_run: bool = False,
              runner=None, now: datetime | None = None, problem_cards: list[int] | None = None,
              progress: list[tuple[str, int]] | None = None) -> BriefResult:
    cfg = {**DEFAULTS, **(settings.brief or {})}
    day = local_day(settings, now)
    existing = db.conn.execute("SELECT status, note FROM briefs WHERE day=?", (day,)).fetchone()
    attempts = int(json.loads(existing["note"] or "{}").get("attempts", 0)) if existing and existing["note"] else 0
    if existing and not force:
        if existing["status"] in ("done", "fallback"):
            return BriefResult(day, "exists", None, 0, note="already written today")
        if attempts >= cfg["max_attempts_per_day"]:
            return BriefResult(day, "exists", None, 0, note=f"gave up after {attempts} attempts")

    topics = select_topics(db, cfg, day)
    if not topics:
        return BriefResult(day, "fallback", None, 0, note="no scored topics; run collect/group/score first")
    enrich_topics(db, [s.topic_id for s in topics[: cfg["enrich_top"]]])
    prompt = build_prompt(settings, db, topics, cfg)
    if dry_run:
        path = settings.briefs_dir / f"{day}.prompt.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt, encoding="utf-8")
        return BriefResult(day, "dry-run", path, len(topics), note=f"{len(prompt)} chars")

    r = call_claude(settings, db, "brief", prompt, SYSTEM, schema=SCHEMA, runner=runner)
    stats = _corpus_stats(db)
    if r.ok and r.data:
        md = render(day, r.data, topics, db, stats, r)
        status = "done"
    else:
        md = render_fallback(day, topics, db, stats, r)
        status = "pending" if r.status in ("limited", "error") else "fallback"
    long_game = progress_entries(settings, db, progress or [])
    md += render_longgame_section(long_game)
    md += render_problems_section(db, problem_cards or [])
    path = settings.briefs_dir / f"{day}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    view = edition_view(day, r.data if r.ok else None, topics, db, stats, r, problem_cards or [])
    view["long_game"] = long_game

    with db.conn:
        db.conn.execute(
            "INSERT OR REPLACE INTO briefs (day, status, model, path, created_at, note, data) VALUES (?,?,?,?,?,?,?)",
            (day, status, r.model, str(path), now_iso(),
             json.dumps({"attempts": attempts + 1, "llm_status": r.status, "error": r.error}), json.dumps(view)),
        )
        if status == "done":
            shown = {_tid(s["topic"]): "top" for s in r.data.get("top_stories", [])}
            shown.update({_tid(q["topic"]): "quick" for q in r.data.get("quick_hits", []) if _tid(q["topic"]) not in shown})
        else:
            shown = {s.topic_id: "top" for s in topics[: cfg["top_stories"]]}
        by_id = {s.topic_id: s for s in topics}
        db.conn.executemany(
            "INSERT OR REPLACE INTO brief_topics (day, topic_id, score, item_count, section) VALUES (?,?,?,?,?)",
            [(day, tid, by_id[tid].score, by_id[tid].features.get("items", 0), sec)
             for tid, sec in shown.items() if tid in by_id],
        )
    return BriefResult(day, status, path, len(topics), r)


def _tid(ref: str) -> int | None:
    try:
        return int(str(ref).strip().lstrip("Tt"))
    except ValueError:
        return None


# ---- rendering ---------------------------------------------------------------

SOURCE_LABEL = {
    "hackernews": "HN", "lobsters": "Lobsters", "reddit": "Reddit", "github": "GitHub", "huggingface": "Hugging Face",
    "arxiv": "arXiv", "bluesky": "Bluesky", "mastodon": "Mastodon", "devto": "dev.to", "discourse": "Forum",
    "stackexchange": "Stack Exchange", "producthunt": "Product Hunt", "rss": "", "pages": "",
}


def topic_links(db: DB, topic_id: int, limit: int = 5) -> str:
    return " · ".join(f"[{s['label']}]({s['url']})" for s in topic_sources(db, topic_id, limit))


def topic_sources(db: DB, topic_id: int, limit: int = 5) -> list[dict]:
    """The topic's best links, most engaged first: [{label, url, original?}].

    `url` is where the discussion happened (HN thread, Reddit post...) or the item itself; `original` is the
    article/repo/paper that discussion is about, when it's a different page.
    """
    rows = db.conn.execute(
        """
        SELECT it.source, it.kind, it.channel, it.url, it.discussion_url, it.metrics FROM topic_items ti
        JOIN items it ON it.id = ti.item_id WHERE ti.topic_id = ?
        """,
        (topic_id,),
    ).fetchall()
    rows = sorted(rows, key=lambda r: -(engagement_value(r["source"], r["kind"], json.loads(r["metrics"]), None) or 0))
    out, seen_urls, seen_labels = [], set(), set()
    for r in rows:
        url = r["discussion_url"] or r["url"]
        label = SOURCE_LABEL.get(r["source"], r["source"]) or r["channel"]
        if r["source"] == "reddit":
            label = r["channel"]
        elif not r["discussion_url"] and r["source"] not in ("rss", "pages", "github", "huggingface", "arxiv"):
            label = _domain(url) or label   # e.g. a Mastodon-trending link to an article: name the site
        label = label or _domain(url) or r["source"]
        m = json.loads(r["metrics"])
        if r["source"] == "hackernews" and m.get("points"):
            label += f" ({m['points']} pts, {m.get('comments', 0)} comments)"
        elif r["source"] == "github" and m.get("stars"):
            label += f" ({m['stars']:,}★)"
        if not url or url in seen_urls or label in seen_labels:
            continue
        seen_urls.add(url)
        seen_labels.add(label)
        entry = {"label": label, "url": url}
        original = r["url"] if r["discussion_url"] and r["url"] and r["url"] != r["discussion_url"] else None
        if original and original not in seen_urls:
            entry["original"] = original
            seen_urls.add(original)
        out.append(entry)
        if len(out) >= limit:
            break
    return out


def edition_view(day: str, data: dict | None, topics: list[TopicScore], db: DB, stats: dict,
                 r: LLMResult | None, problem_cards: list[int]) -> dict:
    """The edition as structured data for the reader UI (stored in briefs.data)."""
    by_id = {s.topic_id: s for s in topics}
    number = db.conn.execute("SELECT COUNT(*) FROM briefs WHERE day < ? AND status IN ('done', 'fallback', 'pending')",
                             (day,)).fetchone()[0] + 1
    view = {"day": day, "number": number, "generated_at": now_iso(), "stats": stats,
            "topics_considered": len(topics), "model": r.model if r and r.ok else None,
            "cost_usd": round(r.cost_usd, 3) if r and r.ok else None,
            "problem_cards": problem_cards, "fallback": not (data and r and r.ok),
            "fallback_reason": None if (data and r and r.ok) else ((r.error if r else None) or "no Claude call")}
    if data and r and r.ok:
        stories = []
        for s in data.get("top_stories", []):
            tid = _tid(s.get("topic"))
            f = by_id[tid].features if tid in by_id else {}
            stories.append({**{k: s.get(k) for k in ("headline", "context", "what", "why_it_matters", "so_what",
                                                     "skeptic", "signal")},
                            "type": f.get("type"), "communities": len(f.get("groups", [])), "items": f.get("items"),
                            "sources": topic_sources(db, tid) if tid is not None else []})
        view.update(
            themes=data.get("themes", []), top_stories=stories,
            quick_hits=[{"line": q["line"], "link": (topic_sources(db, _tid(q.get("topic")), 1) or [None])[0]
                         if _tid(q.get("topic")) is not None else None} for q in data.get("quick_hits", [])],
            study=data.get("study", []),
        )
    else:
        view["fallback_topics"] = [
            {"title": db.conn.execute("SELECT title FROM topics WHERE id=?", (s.topic_id,)).fetchone()[0],
             "type": s.features.get("type"), "score": round(s.score, 3), "items": s.features.get("items"),
             "communities": len(s.features.get("groups", [])), "sources": topic_sources(db, s.topic_id, 3)}
            for s in topics[:20]]
    return view


def _domain(url: str | None) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.") if url else ""


def _corpus_stats(db: DB) -> dict:
    since = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = db.conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT source) FROM items WHERE last_seen_at >= ?", (since,)).fetchone()
    return {"items": row[0], "sources": row[1]}


def _header(day: str, stats: dict, n_topics: int, r: LLMResult | None) -> str:
    date = datetime.fromisoformat(day).strftime("%a %d %b %Y")
    usage = f" · {r.model} · ≈${r.cost_usd:.2f} of usage" if r and r.ok else ""
    return (f"# What's cooking: {date}\n\n"
            f"_{stats['items']:,} items from {stats['sources']} sources in the last 24h · "
            f"{n_topics} top topics considered{usage}_\n")


def render(day: str, data: dict, topics: list[TopicScore], db: DB, stats: dict, r: LLMResult) -> str:
    by_id = {s.topic_id: s for s in topics}
    out = [_header(day, stats, len(topics), r)]
    if data.get("themes"):
        out.append("## Themes today\n")
        out += [f"- {t}" for t in data["themes"]]
        out.append("")
    out.append("## Top stories\n")
    for n, s in enumerate(data.get("top_stories", []), start=1):
        tid = _tid(s.get("topic"))
        kind = by_id[tid].features.get("type") if tid in by_id else ""
        out.append(f"### {n}. {s['headline']}\n")
        out.append(f"`{kind}` · {s['signal']} signal\n" if kind else f"{s['signal']} signal\n")
        if s.get("context"):
            out.append(f"_{s['context']}_\n")
        out.append(f"**What:** {s['what']}\n")
        out.append(f"**Why it matters:** {s['why_it_matters']}\n")
        out.append(f"**So what for you:** {s['so_what']}\n")
        out.append(f"**Skeptic's corner:** {s['skeptic']}\n")
        if tid is not None and (links := topic_links(db, tid)):
            out.append(f"Sources: {links}\n")
    if data.get("quick_hits"):
        out.append("## Quick hits\n")
        for q in data["quick_hits"]:
            tid = _tid(q.get("topic"))
            link = topic_links(db, tid, limit=1) if tid is not None else ""
            out.append(f"- {q['line']}" + (f" {link}" if link else ""))
        out.append("")
    if data.get("study"):
        out.append("## Worth studying\n")
        out += [f"- **{x['concept']}**: {x['why']} Start with: {x['start_with']}" for x in data["study"]]
        out.append("")
    if r.notes:
        out.append(f"\n_Budget notes: {'; '.join(r.notes)}_\n")
    return "\n".join(out).rstrip() + "\n"


def render_longgame_section(entries: list[dict]) -> str:
    if not entries:
        return ""
    out = ["\n## The long game\n", "New releases that move the big, long-running problems forward:\n"]
    for e in entries:
        links = " · ".join(f"[{s['label']}]({s['url']})" for s in e.get("sources", [])[:3])
        out.append(f"### {e['headline']}\n")
        out.append(f"_{e['challenge']} · {e['verdict']}_\n")
        out.append(f"{e['what_changed']} {e['why_it_helps']}\n")
        out.append(f"**For you:** {e['for_you']}\n")
        if links:
            out.append(f"Sources: {links}\n")
    return "\n".join(out)


def render_problems_section(db: DB, card_ids: list[int]) -> str:
    from whats_cooking.pipeline.problems import render_card

    total = db.conn.execute("SELECT COUNT(*) FROM problems WHERE status='open' AND card IS NOT NULL").fetchone()[0]
    out = ["\n## Unsolved problems\n"]
    rows = [db.conn.execute("SELECT * FROM problems WHERE id=?", (pid,)).fetchone() for pid in card_ids]
    rows = [r for r in rows if r and r["card"] and r["status"] == "open"]
    if rows:
        out.append("Newly analysed (or grown a lot) since the last brief:\n")
        out += [render_card(r, json.loads(r["card"])) for r in rows]
    else:
        out.append("No new problem has enough evidence yet.\n")
    out.append(f"Full board: [problems.md](problems.md) ({total} analysed problems).\n")
    return "\n".join(out)


def render_fallback(day: str, topics: list[TopicScore], db: DB, stats: dict, r: LLMResult | None) -> str:
    out = [_header(day, stats, len(topics), None)]
    reason = (r.error if r else None) or "no Claude call"
    retry = (" Run the workflow again to retry; this brief is kept as pending, so you don't need \"Rewrite\"."
             if r and r.status in ("limited", "error") else "")
    out.append(f"> Signals-only brief: Claude wasn't used ({reason[:200]}).{retry}\n")
    out.append("## Top topics by signal\n")
    for n, s in enumerate(topics[:20], start=1):
        title = db.conn.execute("SELECT title FROM topics WHERE id=?", (s.topic_id,)).fetchone()[0]
        f = s.features
        out.append(f"{n}. **{title}** (`{f.get('type')}`, score {s.score:.2f}, {f.get('items')} items across "
                   f"{len(f.get('groups', []))} communities)  \n   {topic_links(db, s.topic_id, limit=3)}")
    return "\n".join(out).rstrip() + "\n"
