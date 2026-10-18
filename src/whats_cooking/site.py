"""Build the reader site: static app files + one data bundle (encrypted for public hosting).

The bundle is encrypted with AES-256-GCM under a key derived from a passphrase (PBKDF2-SHA256,
600k iterations). The browser decrypts it with Web Crypto, so the passphrase never leaves the
device and the hosted files are unreadable without it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from whats_cooking.config import Settings
from whats_cooking.db import DB
from whats_cooking.pipeline.brief import topic_sources
from whats_cooking.pipeline.longgame import longgame_view
from whats_cooking.pipeline.score import latest_scores

KDF_ITERATIONS = 600_000
APP_FILES = ("app.css", "app.js", "robots.txt")
FONTS = ("https://fonts.googleapis.com/css2?family=Newsreader:ital,opsz,wght@0,6..72,300..700;1,6..72,300..600"
         "&family=IBM+Plex+Mono:wght@400;500&display=swap")


def site_src(settings: Settings) -> Path:
    return settings.root / "site"


# ---- data ------------------------------------------------------------------------

def build_bundle(db: DB, max_editions: int = 21, wire_size: int = 40, settings: Settings | None = None) -> dict:
    editions = [json.loads(r[0]) for r in db.conn.execute(
        "SELECT data FROM briefs WHERE data IS NOT NULL ORDER BY day DESC LIMIT ?", (max_editions,))]
    titles = dict(db.conn.execute("SELECT id, title FROM topics"))
    wire = []
    for s in latest_scores(db, limit=wire_size):
        f = s.features
        links = topic_sources(db, s.topic_id, 3)
        wire.append({"id": s.topic_id, "title": titles.get(s.topic_id, ""), "type": f.get("type"),
                     "score": round(s.score, 3), "communities": f.get("groups", []), "items": f.get("items"),
                     "comments": f.get("comments"), "age_hours": f.get("age_hours"), "sources": links})
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "editions": editions,
        "problems": _problems(db),
        "long_game": longgame_view(settings, db) if settings else [],
        "wire": wire,
        "communities": _communities(db, settings),
        "index": _index(db),
        "topics_scored": db.conn.execute(
            "SELECT COUNT(*) FROM topic_scores WHERE scored_at=(SELECT MAX(scored_at) FROM topic_scores)").fetchone()[0],
        "collection": _collection(db),
        # Where to ask for a new edition: GitHub's page for this workflow, with its "Run workflow" button.
        # GITHUB_REPOSITORY is set automatically inside GitHub Actions.
        "request_url": (f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/workflows/daily.yml"
                        if os.environ.get("GITHUB_REPOSITORY") else None),
    }


SOURCE_NAMES = {"hackernews": "HN thread", "lobsters": "Lobsters thread", "github": "GitHub issue",
                "discourse": "Forum thread", "stackexchange": "Stack Exchange question", "reddit": "Reddit post",
                "bluesky": "Bluesky post", "mastodon": "Mastodon post", "yc_rfs": "YC Request for Startups"}


def problem_sources(db: DB, problem_id: int, limit: int = 8) -> list[dict]:
    """Where a problem was raised: the threads/issues (most people first), plus what each thread was about."""
    rows = db.conn.execute(
        """
        SELECT pm.source, pm.url, MAX(pm.people) AS people, it.url AS item_url, it.discussion_url, it.channel
        FROM pain_mentions pm LEFT JOIN items it ON it.id = pm.item_id
        WHERE pm.problem_id = ? AND pm.url IS NOT NULL
        GROUP BY pm.item_id ORDER BY people DESC LIMIT ?
        """,
        (problem_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        label = SOURCE_NAMES.get(r["source"], r["source"])
        if r["source"] == "discourse" and r["channel"]:
            label = f"{r['channel']} thread"
        if r["people"] > 1:
            label += f" (~{r['people']} people)"
        entry = {"label": label, "url": r["url"]}
        if r["discussion_url"] and r["item_url"] and r["item_url"] != r["discussion_url"]:
            entry["original"] = r["item_url"]
        out.append(entry)
    return out


def _problems(db: DB) -> dict:
    def row(p) -> dict:
        return {"id": p["id"], "kind": p["kind"], "title": p["title"], "people": p["people"], "authors": p["authors"],
                "threads": p["threads"], "days_active": p["days_active"], "communities": json.loads(p["communities"]),
                "companies": p["company_mentions"], "workarounds": p["workarounds"], "score": round(p["score"], 4),
                "card": json.loads(p["card"]) if p["card"] else None, "card_at": p["card_at"],
                "sources": problem_sources(db, p["id"])}
    analysed = [row(p) for p in db.conn.execute(
        "SELECT * FROM problems WHERE status='open' AND card IS NOT NULL ORDER BY score DESC")]
    emerging = [row(p) for p in db.conn.execute(
        """SELECT * FROM problems WHERE status='open' AND card IS NULL AND kind='user' AND (authors >= 2 OR people >= 20)
           ORDER BY authors DESC, people DESC LIMIT 12""")]
    pending_requests = [row(p) for p in db.conn.execute(
        "SELECT * FROM problems WHERE status='open' AND card IS NULL AND kind='request' ORDER BY title")]
    return {"analysed": [p for p in analysed if p["kind"] == "user"],
            "requests": [p for p in analysed if p["kind"] == "request"] + pending_requests,
            "emerging": emerging}


# ---- per-community views: what's top in each big community right now --------------------------

COMMUNITIES = {
    "hackernews": {"label": "Hacker News", "home": "https://news.ycombinator.com/"},
    "github": {"label": "GitHub", "home": "https://github.com/trending"},
    "reddit": {"label": "Reddit", "home": "https://www.reddit.com/"},
    "huggingface": {"label": "Hugging Face", "home": "https://huggingface.co/papers"},
}


def _item_entry(r) -> dict:
    m = json.loads(r["metrics"])
    bits = []
    for key, word in (("points", "points"), ("comments", "comments"), ("stars_today", "stars today"),
                      ("stars", "stars"), ("thumbs_up", "👍"), ("upvotes", "upvotes"), ("likes", "likes"),
                      ("day_rank", "rank today")):
        if m.get(key):
            bits.append(f"{m[key]:,} {word}" if key != "day_rank" else f"#{m[key]} today")
    entry = {"title": r["title"], "url": r["discussion_url"] or r["url"], "meta": " · ".join(bits[:3]),
             "channel": r["channel"]}
    if r["discussion_url"] and r["url"] and r["url"] != r["discussion_url"]:
        entry["original"] = r["url"]
    return entry


def _communities(db: DB, settings: Settings | None) -> dict:
    since = "datetime('now', '-30 hours')"
    q = lambda where, order, limit: [_item_entry(r) for r in db.conn.execute(
        f"SELECT title, url, discussion_url, metrics, channel FROM items WHERE last_seen_at >= strftime('%Y-%m-%dT%H:%M:%SZ', {since}) "
        f"AND {where} ORDER BY {order} LIMIT {limit}")]
    num = lambda k: f"CAST(COALESCE(json_extract(metrics, '$.{k}'), 0) AS REAL)"
    out = {}
    out["hackernews"] = {**COMMUNITIES["hackernews"], "sections": [
        {"title": "Top stories", "home": "https://news.ycombinator.com/",
         "items": q("source='hackernews' AND kind='story'", f"{num('front_page')} DESC, {num('points')} DESC", 30)},
        {"title": "Show HN", "home": "https://news.ycombinator.com/show",
         "items": q("source='hackernews' AND kind='show'", f"{num('points')} DESC", 12)},
        {"title": "Ask HN", "home": "https://news.ycombinator.com/ask",
         "items": q("source='hackernews' AND kind='ask'", f"{num('points')} DESC", 10)},
    ]}
    out["github"] = {**COMMUNITIES["github"], "sections": [
        {"title": "Trending today", "home": "https://github.com/trending",
         "items": q("source='github' AND kind='repo' AND json_extract(metrics, '$.stars_today') IS NOT NULL",
                    f"{num('stars_today')} DESC", 25)},
        {"title": "New this week, rising fast", "home": "https://github.com/search?q=stars%3A%3E50&s=stars&type=repositories",
         "items": q("source='github' AND kind='repo' AND channel='new_repos'", f"{num('stars')} DESC", 20)},
        {"title": "Most-wanted open issues", "home": None,
         "items": q("source='github' AND kind='issue'", f"{num('thumbs_up')} DESC", 12)},
        {"title": "Releases", "home": None, "items": q("source='github' AND kind='release'", "created_at DESC", 12)},
    ]}
    subs = (settings.source_cfg("reddit").get("subreddits", []) if settings else [])
    out["reddit"] = {**COMMUNITIES["reddit"], "sections": [
        {"title": f"r/{s}", "home": f"https://www.reddit.com/r/{s}/top/?t=day",
         "items": q(f"source='reddit' AND channel='r/{s}'", f"{num('day_rank')} ASC", 4)} for s in subs]}
    out["huggingface"] = {**COMMUNITIES["huggingface"], "sections": [
        {"title": "Papers of the day", "home": "https://huggingface.co/papers",
         "items": q("source='huggingface' AND kind='paper'", f"{num('upvotes')} DESC", 15)},
        {"title": "Trending models", "home": "https://huggingface.co/models?sort=trending",
         "items": q("source='huggingface' AND kind='model'", f"{num('trending_score')} DESC", 15)},
        {"title": "Trending Spaces (demos)", "home": "https://huggingface.co/spaces?sort=trending",
         "items": q("source='huggingface' AND kind='space'", f"{num('trending_score')} DESC", 10)},
        {"title": "Trending datasets", "home": "https://huggingface.co/datasets?sort=trending",
         "items": q("source='huggingface' AND kind='dataset'", f"{num('trending_score')} DESC", 8)},
    ]}
    for c in out.values():
        c["sections"] = [s for s in c["sections"] if s["items"]]
    return out


def _index(db: DB, limit: int = 400) -> list[dict]:
    """Every scored topic's title and rank, so you can search for something you saw elsewhere."""
    titles = dict(db.conn.execute("SELECT id, title FROM topics"))
    out = []
    for rank, s in enumerate(latest_scores(db, limit=limit), start=1):
        links = topic_sources(db, s.topic_id, 2)
        out.append({"rank": rank, "title": titles.get(s.topic_id, ""), "communities": s.features.get("groups", []),
                    "items": s.features.get("items"), "url": links[0]["url"] if links else None})
    return out


def _collection(db: DB) -> dict:
    row = db.conn.execute(
        "SELECT MAX(started_at) FROM runs WHERE kind='collect' AND finished_at IS NOT NULL").fetchone()
    sources = db.conn.execute(
        "SELECT COUNT(DISTINCT source) FROM items WHERE last_seen_at >= datetime('now', '-1 day')").fetchone()[0]
    return {"last_run": row[0] if row else None, "sources": sources}


# ---- encryption ----------------------------------------------------------------------

def encrypt(payload: bytes, passphrase: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, iv = os.urandom(16), os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, KDF_ITERATIONS, dklen=32)
    ct = AESGCM(key).encrypt(iv, payload, None)   # ciphertext || 16-byte tag, as Web Crypto expects
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": KDF_ITERATIONS, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decrypt(blob: dict, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    d = lambda s: base64.b64decode(s)
    key = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), d(blob["salt"]), blob["iter"], dklen=32)
    return AESGCM(key).decrypt(d(blob["iv"]), d(blob["ct"]), None)


# ---- output ----------------------------------------------------------------------------

def _page(settings: Settings, data_tag: str = "") -> str:
    """index.html: a full document wrapping site/body.html."""
    src = site_src(settings)
    body = (src / "body.html").read_text(encoding="utf-8")
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">\n"
        "<meta name=\"robots\" content=\"noindex,nofollow\">\n<title>What's Cooking</title>\n"
        f"<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">\n"
        f"<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>\n"
        f"<link rel=\"stylesheet\" href=\"{FONTS}\">\n<link rel=\"stylesheet\" href=\"app.css\">\n"
        f"</head>\n<body>\n{body}\n{data_tag}<script src=\"app.js\"></script>\n</body>\n</html>\n"
    )


def build_site(settings: Settings, db: DB, out: Path, passphrase: str | None) -> dict:
    """Write the deployable site to `out`. Encrypted when a passphrase is given."""
    out.mkdir(parents=True, exist_ok=True)
    for f in APP_FILES:
        shutil.copyfile(site_src(settings) / f, out / f)
    (out / "index.html").write_text(_page(settings), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    bundle = build_bundle(db, settings=settings)
    payload = json.dumps(bundle, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if passphrase:
        (out / "data.enc.json").write_text(json.dumps(encrypt(payload, passphrase)), encoding="utf-8")
        (out / "data.json").unlink(missing_ok=True)
    else:
        (out / "data.json").write_bytes(payload)
        (out / "data.enc.json").unlink(missing_ok=True)
    return {"editions": len(bundle["editions"]), "problems": len(bundle["problems"]["analysed"]),
            "wire": len(bundle["wire"]), "bytes": len(payload), "encrypted": bool(passphrase)}


def build_preview(settings: Settings, bundle: dict) -> str:
    """One self-contained page (CSS, JS and data inline) for a private preview."""
    src = site_src(settings)
    css = (src / "app.css").read_text(encoding="utf-8")
    js = (src / "app.js").read_text(encoding="utf-8")
    body = (src / "body.html").read_text(encoding="utf-8")
    data = json.dumps(bundle, ensure_ascii=False).replace("</", "<\\/")
    return (f"<title>What's Cooking</title>\n<link rel=\"stylesheet\" href=\"{FONTS}\">\n<style>\n{css}\n</style>\n"
            f"{body}\n<script>window.__WC_DATA__ = {data};</script>\n<script>\n{js}\n</script>\n")
