"""Run every enabled collector concurrently and store the results."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from whats_cooking.collectors import REGISTRY, Context
from whats_cooking.config import Settings
from whats_cooking.db import DB, now_iso
from whats_cooking.http import Fetcher
from whats_cooking.models import Item

log = logging.getLogger(__name__)


@dataclass
class CollectorResult:
    name: str
    status: str
    items: int = 0
    new_items: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: str | None = None


def enabled_collectors(settings: Settings, only: list[str] | None = None, skip: list[str] | None = None) -> list[str]:
    names = [n for n in REGISTRY if settings.source_cfg(n).get("enabled", False)]
    if only:
        unknown = set(only) - set(REGISTRY)
        if unknown:
            raise ValueError(f"unknown collector(s): {', '.join(sorted(unknown))}")
        names = [n for n in only]
    if skip:
        names = [n for n in names if n not in skip]
    return names


def run_collect(settings: Settings, db: DB, only: list[str] | None = None,
                skip: list[str] | None = None, workers: int = 6) -> tuple[int, list[CollectorResult]]:
    run_id = db.start_run("collect")
    fetcher = Fetcher()
    ctx = Context(
        http=fetcher,
        known_channels=frozenset(db.known_channels()),
        watch={k: db.watchlist(k) for k in ("github_repo", "bluesky_account")},
    )
    names = enabled_collectors(settings, only, skip)
    results: list[CollectorResult] = []

    def work(name: str):
        started = now_iso()
        collector = REGISTRY[name](settings.source_cfg(name), ctx)
        if reason := collector.skip_reason():
            return name, started, collector, [], reason
        items: list[Item] = []
        try:
            for item in collector.collect():
                if item.title and item.title.strip():
                    items.append(item)
        except Exception as exc:  # a bug in a collector must not kill the run
            log.exception("collector %s crashed", name)
            collector.fail("crash", exc)
        return name, started, collector, items, None

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, n) for n in names]
            for fut in as_completed(futures):
                name, started, collector, items, skipped = fut.result()
                if skipped:
                    db.record_collector(run_id, name, started, "skipped", error=skipped)
                    results.append(CollectorResult(name, "skipped", skipped=skipped))
                    continue
                total, new = db.upsert_items(items, run_id)
                if not collector.errors:
                    status = "ok"
                elif items:
                    status = "partial"
                else:
                    status = "error"
                db.record_collector(run_id, name, started, status, total, new,
                                    "\n".join(collector.errors) or None)
                results.append(CollectorResult(name, status, total, new, collector.errors))
                log.info("%-14s %-7s items=%-5d new=%-5d errors=%d", name, status, total, new, len(collector.errors))
    finally:
        fetcher.close()
        db.finish_run(run_id)
    results.sort(key=lambda r: r.name)
    return run_id, results
