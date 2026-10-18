"""One run of the pipeline.

Every run (no Claude):   collect -> group -> score -> mine complaints -> group/score problems
                         -> long game: match topics to big challenges, fill in trend history -> watch lists -> prune
When a brief is wanted:  + dedupe (Haiku) -> problem cards (Sonnet, new/growing problems only)
                         -> long game judging (Sonnet, capped) -> brief (Sonnet) -> problems board
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from whats_cooking.config import Settings
from whats_cooking.db import DB
from whats_cooking.pipeline.brief import BriefResult, local_day, run_brief
from whats_cooking.pipeline.collect import CollectorResult, run_collect
from whats_cooking.pipeline.dedupe import run_dedupe
from whats_cooking.pipeline.group import GroupStats, run_group
from whats_cooking.pipeline.longgame import judge, match_topics, refresh_trends
from whats_cooking.pipeline.pains import harvest
from whats_cooking.pipeline.problems import assign_problems, score_problems, write_board, write_cards
from whats_cooking.pipeline.prune import run_prune
from whats_cooking.pipeline.score import run_score
from whats_cooking.pipeline.watch import WatchChanges, update_bluesky_watch, update_repo_watch

log = logging.getLogger(__name__)


@dataclass
class DailyReport:
    collected: list[CollectorResult] = field(default_factory=list)
    grouped: GroupStats | None = None
    scored: int = 0
    merged: int = 0
    mentions: int = 0
    problems: dict = field(default_factory=dict)
    cards: list[int] = field(default_factory=list)
    challenge_matches: int = 0
    trend_requests: int = 0
    progress: list = field(default_factory=list)
    repos: WatchChanges | None = None
    accounts: WatchChanges | None = None
    brief: BriefResult | None = None
    pruned: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def brief_due(db: DB, settings: Settings, mode: str, force: bool = False) -> bool:
    if mode == "always":
        if force:
            return True
        # Today's edition is already written: don't spend Claude usage on dedupe, cards or judging again.
        row = db.conn.execute("SELECT status FROM briefs WHERE day=?", (local_day(settings),)).fetchone()
        return not (row and row[0] in ("done", "fallback"))
    if mode == "if-pending":
        row = db.conn.execute("SELECT status FROM briefs WHERE day=?", (local_day(settings),)).fetchone()
        return bool(row and row[0] == "pending")
    return False


def data_is_stale(db: DB, max_age_hours: float = 12) -> bool:
    row = db.conn.execute("SELECT MAX(last_seen_at) FROM items").fetchone()
    if not row or not row[0]:
        return True
    newest = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    return datetime.now(UTC) - newest > timedelta(hours=max_age_hours)


def run_daily(settings: Settings, db: DB, brief_mode: str = "never", skip: list[str] | None = None,
              prune: bool = True, collect: bool = True, force_brief: bool = False) -> DailyReport:
    rep = DailyReport()
    if not collect and data_is_stale(db):
        log.info("no recent collection in the database; collecting first")
        collect = True

    def step(name, fn):
        try:
            return fn()
        except Exception as exc:  # one failing step must not lose the rest of the run
            log.exception("step %s failed", name)
            rep.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            return None

    if collect:
        _, rep.collected = step("collect", lambda: run_collect(settings, db, skip=skip)) or (None, [])
        rep.grouped = step("group", lambda: run_group(settings, db))
    scores = step("score", lambda: run_score(settings, db)) or []
    rep.scored = len(scores)
    if collect:
        rep.mentions = step("mine complaints", lambda: harvest(settings, db)) or 0
        rep.problems = step("group problems", lambda: assign_problems(settings, db)) or {}
    step("score problems", lambda: score_problems(settings, db))
    if scores:
        rep.challenge_matches = step("long game: match", lambda: match_topics(settings, db, scores)) or 0
    if collect:
        rep.trend_requests = step("long game: history", lambda: refresh_trends(settings, db)) or 0

    wants_brief = bool(scores) and brief_due(db, settings, brief_mode, force_brief)
    if brief_mode == "always" and not wants_brief and scores:
        log.info("today's edition already exists; skipping all Claude steps (tick 'Rewrite' to redo it)")
    if wants_brief:
        rep.merged = step("dedupe", lambda: run_dedupe(settings, db, scores)) or 0
        if rep.merged:
            scores = step("rescore", lambda: run_score(settings, db)) or scores
    if scores:
        rep.repos = step("watch repos", lambda: update_repo_watch(settings, db, scores))
    rep.accounts = step("watch bluesky", lambda: update_bluesky_watch(settings, db))
    if wants_brief:
        rep.cards = (step("problem cards", lambda: write_cards(settings, db)) or ([], None))[0]
        rep.progress = (step("long game: judge", lambda: judge(settings, db)) or ([], None))[0]
        rep.brief = step("brief", lambda: run_brief(settings, db, force=force_brief, problem_cards=rep.cards,
                                                    progress=rep.progress))
        step("problems board", lambda: write_board(settings, db))
    if prune:
        rep.pruned = step("prune", lambda: run_prune(settings, db)) or {}
    return rep
