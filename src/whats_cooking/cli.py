"""Command line: `whats-cooking <command>` or `python -m whats_cooking <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from whats_cooking.config import load_settings
from whats_cooking.db import DB


def _csv(v: str | None) -> list[str] | None:
    return [s.strip() for s in v.split(",") if s.strip()] if v else None


def cmd_collect(args, settings, db) -> int:
    from whats_cooking.pipeline.collect import run_collect

    t0 = time.time()
    run_id, results = run_collect(settings, db, only=_csv(args.only), skip=_csv(args.skip))
    print(f"\nRun {run_id} finished in {time.time() - t0:.0f}s\n")
    print(f"{'collector':<15}{'status':<9}{'items':>7}{'new':>7}  notes")
    for r in results:
        note = r.skipped or (f"{len(r.errors)} error(s): {r.errors[0][:70]}" if r.errors else "")
        print(f"{r.name:<15}{r.status:<9}{r.items:>7}{r.new_items:>7}  {note}")
    print(f"{'total':<24}{sum(r.items for r in results):>7}{sum(r.new_items for r in results):>7}")
    return 1 if all(r.status == "error" for r in results if r.status != "skipped") else 0


def cmd_group(args, settings, db) -> int:
    from whats_cooking.pipeline.group import run_group

    t0 = time.time()
    s = run_group(settings, db)
    print(f"Grouped in {time.time() - t0:.0f}s: embedded={s.embedded} assigned={s.assigned} "
          f"(by ref={s.by_ref}, by similarity={s.by_similarity}, new topics={s.new_topics}, "
          f"merged={s.merged_topics}, baseline skipped={s.skipped_baseline})")
    return 0


def cmd_run(args, settings, db) -> int:
    rc = cmd_collect(args, settings, db)
    print()
    return cmd_group(args, settings, db) or rc


def cmd_status(args, settings, db) -> int:
    run = db.conn.execute("SELECT * FROM runs WHERE kind='collect' ORDER BY id DESC LIMIT 1").fetchone()
    if not run:
        print("No runs yet. Try: whats-cooking run")
        return 0
    print(f"Last collect run {run['id']}: {run['started_at']} -> {run['finished_at']}\n")
    for r in db.conn.execute("SELECT * FROM collector_runs WHERE run_id=? ORDER BY collector", (run["id"],)):
        err = (r["error"] or "").split("\n")[0][:80]
        print(f"{r['collector']:<15}{r['status']:<9}{r['items']:>7}{r['new_items']:>7}  {err}")
    counts = db.conn.execute(
        "SELECT (SELECT COUNT(*) FROM items), (SELECT COUNT(*) FROM topics WHERE merged_into IS NULL), "
        "(SELECT COUNT(*) FROM topics WHERE merged_into IS NULL AND source_count >= 2)"
    ).fetchone()
    print(f"\nItems: {counts[0]}  Topics: {counts[1]}  Multi-source topics: {counts[2]}")
    return 0


def cmd_topics(args, settings, db) -> int:
    rows = db.conn.execute(
        """
        SELECT id, title, item_count, source_count, sources, last_seen_at FROM topics
        WHERE merged_into IS NULL AND source_count >= ?
        ORDER BY source_count DESC, item_count DESC, last_seen_at DESC LIMIT ?
        """,
        (args.min_sources, args.limit),
    ).fetchall()
    for t in rows:
        srcs = ", ".join(f"{k}:{v}" for k, v in json.loads(t["sources"]).items())
        print(f"[{t['id']}] {t['title'][:100]}\n      {t['item_count']} items / {t['source_count']} sources ({srcs})")
        if args.items:
            for it in db.conn.execute(
                "SELECT it.source, it.title, it.url, ti.method, ti.similarity FROM topic_items ti "
                "JOIN items it ON it.id = ti.item_id WHERE ti.topic_id=? LIMIT 12", (t["id"],)):
                sim = f"{it['similarity']:.2f}" if it["similarity"] is not None else "  - "
                print(f"        {it['method']:<9}{sim} {it['source']:<13} {it['title'][:80]}")
    return 0


def cmd_score(args, settings, db) -> int:
    from whats_cooking.pipeline.score import run_score

    scores = run_score(settings, db)
    print(f"Scored {len(scores)} topics.")
    return cmd_top(args, settings, db)


def cmd_top(args, settings, db) -> int:
    from whats_cooking.pipeline.score import latest_scores

    titles = dict(db.conn.execute("SELECT id, title FROM topics"))
    for s in latest_scores(db, limit=args.limit):
        f = s.features
        print(f"{s.score:.3f}  {f.get('type', ''):<8} {len(f.get('groups', [])):>2} comm  {f.get('items'):>3} items  "
              f"[{s.topic_id}] {titles.get(s.topic_id, '')[:80]}")
    return 0


def cmd_brief(args, settings, db) -> int:
    from whats_cooking.pipeline.brief import run_brief
    from whats_cooking.pipeline.dedupe import run_dedupe
    from whats_cooking.pipeline.problems import write_board, write_cards
    from whats_cooking.pipeline.score import latest_scores, run_score

    cards: list[int] = []
    if not args.dry_run:
        if not args.no_dedupe:
            merged = run_dedupe(settings, db, latest_scores(db))
            if merged:
                print(f"Merged {merged} duplicate topic(s); rescoring.")
                run_score(settings, db)
        cards, cr = write_cards(settings, db)
        if cr:
            print(f"Problem cards: {len(cards)} written ({cr.status}, ≈${cr.cost_usd:.3f})")
        from whats_cooking.pipeline.longgame import judge
        progress, lr = judge(settings, db)
        if lr:
            print(f"Long game: {len(progress)} progress entries ({lr.status}, ≈${lr.cost_usd:.3f})")
    else:
        progress = []
    r = run_brief(settings, db, force=args.force, dry_run=args.dry_run, problem_cards=cards, progress=progress)
    if not args.dry_run:
        write_board(settings, db)
    else:
        from whats_cooking.pipeline.problems import cards_due, cards_prompt
        due = cards_due(settings, db)
        if due:
            path = settings.briefs_dir / f"{r.day}.problems.prompt.md"
            path.write_text(cards_prompt(settings, db, due), encoding="utf-8")
            print(f"Problem-cards prompt ({len(due)} problems) written to {path}")
    print(f"Brief {r.day}: {r.status} ({r.topics} topics) {r.note}")
    if r.llm:
        print(f"Claude: {r.llm.status}, model {r.llm.model}, ≈${r.llm.cost_usd:.3f}, "
              f"{r.llm.input_tokens} in / {r.llm.output_tokens} out tokens" + (f", {r.llm.error}" if r.llm.error else ""))
        for n in r.llm.notes:
            print(f"  note: {n}")
    if r.path:
        print(f"Written to {r.path}")
    return 0 if r.status in ("done", "exists", "dry-run") else 1


def cmd_daily(args, settings, db) -> int:
    from whats_cooking.pipeline.daily import run_daily

    t0 = time.time()
    rep = run_daily(settings, db, brief_mode=args.brief, skip=_csv(args.skip), prune=not args.no_prune,
                    collect=not args.no_collect, force_brief=args.force_brief)
    print(f"\nDaily run finished in {time.time() - t0:.0f}s")
    ok = [r for r in rep.collected if r.status == "ok"]
    print(f"  collect: {sum(r.items for r in rep.collected)} items, {sum(r.new_items for r in rep.collected)} new; "
          f"{len(ok)}/{len(rep.collected)} sources ok")
    for r in rep.collected:
        if r.status not in ("ok", "skipped"):
            print(f"    {r.name}: {r.status}: {r.errors[0][:90] if r.errors else ''}")
    if rep.grouped:
        g = rep.grouped
        print(f"  group: {g.assigned} assigned, {g.new_topics} new topics, {g.merged_topics} merged")
    print(f"  score: {rep.scored} topics; dedupe merged {rep.merged}")
    if rep.problems or rep.mentions:
        print(f"  problems: {rep.mentions} new complaint mentions; {rep.problems}; cards written {len(rep.cards)}")
    for label, ch in (("repos", rep.repos), ("bluesky", rep.accounts)):
        if ch and (ch.added or ch.dropped):
            print(f"  watch {label}: +{len(ch.added)} {ch.added[:5]} -{len(ch.dropped)} {ch.dropped[:5]}")
    if rep.brief:
        print(f"  brief: {rep.brief.status} -> {rep.brief.path}")
    if rep.pruned:
        print(f"  prune: {rep.pruned}")
    for e in rep.errors:
        print(f"  ERROR {e}")
    return 1 if rep.errors else 0


def cmd_budget(args, settings, db) -> int:
    from whats_cooking.llm import BudgetGuard

    g = BudgetGuard(settings, db)
    day, week = g.spent(24), g.spent(24 * 7)
    print(f"Last 24h: ${day:.3f} of ${g.cfg['daily_cap_usd']:.2f}   Last 7d: ${week:.3f} of ${g.cfg['weekly_cap_usd']:.2f} "
          f"({week / g.cfg['weekly_cap_usd']:.0%})")
    print("(API-equivalent dollars: a yardstick for how much of your Pro usage the tool took. Nothing is charged.)\n")
    for p in ("brief", "dedupe", "problems", "longgame", "weekly"):
        model, cap, notes = g.plan(p)
        print(f"  next {p:<7} -> {model or 'SKIP'}" + (f" (cap ${cap:.2f})" if model else "") + (f"  {'; '.join(notes)}" if notes else ""))
    rows = db.conn.execute("SELECT * FROM llm_calls ORDER BY id DESC LIMIT ?", (args.limit,)).fetchall()
    if rows:
        print("\nRecent calls:")
    for r in rows:
        print(f"  {r['ts']} {r['purpose']:<7} {r['model']:<26} {r['status']:<8} ${r['cost_usd']:.3f} "
              f"{r['input_tokens']}/{r['output_tokens']} tok" + (f"  {r['error'][:60]}" if r['error'] else ""))
    return 0


def cmd_problems(args, settings, db) -> int:
    from whats_cooking.pipeline.problems import DEFAULTS, eligible, score_problems

    score_problems(settings, db)
    cfg = {**DEFAULTS, **(settings.brief.get("problems") or {})}
    rows = db.conn.execute("SELECT * FROM problems WHERE status='open' ORDER BY score DESC LIMIT ?", (args.limit,)).fetchall()
    for r in rows:
        flag = "card" if r["card"] else ("ready" if eligible(r, cfg) else "     ")
        title = json.loads(r["card"]).get("title") if r["card"] else r["title"]
        print(f"{r['score']:.3f} {flag} {r['kind']:<7} {r['people']:>4}p {r['authors']:>3}a {r['threads']:>3}t "
              f"[P{r['id']}] {title[:90]}")
    return 0


def cmd_site(args, settings, db) -> int:
    import os
    from pathlib import Path

    from whats_cooking.site import build_site

    passphrase = None
    if not args.plain:
        passphrase = os.environ.get(args.passphrase_env)
        if not passphrase:
            print(f"Set {args.passphrase_env} to encrypt the site, or pass --plain for a local, unencrypted build.")
            return 1
    out = Path(args.out)
    info = build_site(settings, db, out, passphrase)
    print(f"Site written to {out}: {info}")
    if args.serve:
        import functools
        import http.server
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(out))
        print(f"Serving on http://localhost:{args.port} (Ctrl+C to stop)")
        http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler).serve_forever()
    return 0


def cmd_longgame(args, settings, db) -> int:
    from whats_cooking.pipeline.longgame import longgame_view, match_topics, refresh_trends
    from whats_cooking.pipeline.score import latest_scores

    if args.history:
        print(f"History: {refresh_trends(settings, db, budget_s=args.budget)} searches made")
    match_topics(settings, db, latest_scores(db))
    for c in longgame_view(settings, db):
        counts = [p["count"] for p in c["trend"] if p["count"] is not None]
        change = f"{c['change_pct']:+d}% vs previous 6 months" if c["change_pct"] is not None else "no history yet"
        print(f"{'★' if c['focus'] else ' '} {c['title']:<48} {change:<32} "
              f"{len(c['entries'])} progress, {len(c['watching'])} watching, {c['complaints']['people']} people complaining")
        if args.verbose_items:
            for w in c["watching"]:
                print(f"      watching: {w['title'][:90]}")
    return 0


def cmd_watch(args, settings, db) -> int:
    for r in db.conn.execute("SELECT * FROM watchlist ORDER BY kind, active DESC, last_hit_at DESC"):
        state = "active " if r["active"] else "dropped"
        print(f"{r['kind']:<16} {state} {r['value']:<40} last hit {r['last_hit_at'][:10]}  {r['reason'][:60]}")
    return 0


def cmd_prune(args, settings, db) -> int:
    from whats_cooking.pipeline.prune import run_prune

    print(run_prune(settings, db))
    return 0


def cmd_sources(args, settings, db) -> int:
    from whats_cooking.collectors import REGISTRY

    for name in REGISTRY:
        cfg = settings.source_cfg(name)
        state = "enabled" if cfg.get("enabled") else "disabled"
        print(f"{name:<15}{state}{'  sunset ' + str(cfg['sunset']) if cfg.get('sunset') else ''}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="whats-cooking")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("collect", "run"):
        p = sub.add_parser(name, help="fetch all sources" + (" and group into topics" if name == "run" else ""))
        p.add_argument("--only", help="comma-separated collectors to run")
        p.add_argument("--skip", help="comma-separated collectors to skip")
    sub.add_parser("group", help="embed new items and group them into topics")
    sub.add_parser("status", help="show the last run's per-source results")
    sub.add_parser("sources", help="list collectors and whether they're enabled")
    p = sub.add_parser("topics", help="list topics, most corroborated first")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--min-sources", type=int, default=2)
    p.add_argument("--items", action="store_true", help="show member items")
    for name, help_ in (("score", "score topics by importance, then show the top"), ("top", "show the latest top-scored topics")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("brief", help="write today's brief with Claude (budget-guarded)")
    p.add_argument("--force", action="store_true", help="rewrite even if today's brief exists")
    p.add_argument("--dry-run", action="store_true", help="only write the prompt to briefs/<day>.prompt.md")
    p.add_argument("--no-dedupe", action="store_true", help="skip the duplicate-merge check")
    p = sub.add_parser("daily", help="full scheduled run: collect, group, score, watch lists, brief, prune")
    p.add_argument("--brief", choices=["always", "if-pending", "never"], default="never")
    p.add_argument("--skip", help="comma-separated collectors to skip")
    p.add_argument("--no-prune", action="store_true")
    p.add_argument("--no-collect", action="store_true", help="use already-collected data (fast on-demand brief)")
    p.add_argument("--force-brief", action="store_true", help="rewrite today's brief even if it exists")
    p = sub.add_parser("problems", help="tracked problems: score, people, authors, threads")
    p.add_argument("--limit", type=int, default=30)
    p = sub.add_parser("budget", help="Claude usage vs. caps, and recent calls")
    p.add_argument("--limit", type=int, default=10)
    sub.add_parser("watch", help="show the self-updating watch lists")
    p = sub.add_parser("longgame", help="the big long-term challenges: interest trend, progress, candidates")
    p.add_argument("--history", action="store_true", help="fill in the interest-over-time history first")
    p.add_argument("--budget", type=float, default=300, help="seconds to spend on history")
    p.add_argument("--items", dest="verbose_items", action="store_true", help="list candidates being watched")
    p = sub.add_parser("site", help="build the reader site (encrypted unless --plain)")
    p.add_argument("--out", default="build/site")
    p.add_argument("--passphrase-env", default="SITE_PASSPHRASE", help="env var holding the passphrase")
    p.add_argument("--plain", action="store_true", help="no encryption (local viewing only, never publish this)")
    p.add_argument("--serve", action="store_true", help="serve it on localhost after building")
    p.add_argument("--port", type=int, default=8765)
    sub.add_parser("prune", help="apply retention rules and compact the database")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    settings = load_settings()
    db = DB(settings.db_path)
    try:
        handler = {
            "collect": cmd_collect, "group": cmd_group, "run": cmd_run, "status": cmd_status,
            "topics": cmd_topics, "sources": cmd_sources, "score": cmd_score, "top": cmd_top,
            "brief": cmd_brief, "daily": cmd_daily, "budget": cmd_budget, "watch": cmd_watch, "prune": cmd_prune,
            "problems": cmd_problems, "site": cmd_site, "longgame": cmd_longgame,
        }[args.command]
        return handler(args, settings, db)
    finally:
        db.close()
