"""Generic RSS/Atom: newsletters, blogs, lab blogs, news sites, YouTube channels, podcasts."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import feedparser

from whats_cooking.collectors.base import Collector, parse_dt, register, strip_html, truncate
from whats_cooking.models import Item


@register
class RSS(Collector):
    name = "rss"

    def collect(self):
        feeds = self.cfg.get("feeds", [])
        with ThreadPoolExecutor(max_workers=self.cfg.get("workers", 8)) as pool:
            results = list(pool.map(self._fetch, feeds))
        for feed, parsed in zip(feeds, results):
            if parsed is not None:
                yield from self._items(feed, parsed)

    def _fetch(self, feed: dict):
        try:
            parsed = feedparser.parse(self.http.get(feed["url"]).content)
            if parsed.bozo and not parsed.entries:
                raise ValueError(f"unparseable feed: {parsed.get('bozo_exception')}")
            return parsed
        except Exception as exc:
            self.fail(feed["name"], exc)
            return None

    def _items(self, feed: dict, parsed):
        lookback = timedelta(hours=feed.get("lookback_hours", self.cfg.get("lookback_hours", 72)))
        since = self.ctx.now - lookback
        kind = feed.get("kind", "article")
        for e in parsed.entries[: feed.get("max_items", 30)]:
            created = parse_dt(e.get("published_parsed") or e.get("updated_parsed") or e.get("published"))
            if created is None or created < since:
                continue
            link = e.get("link")
            uid = e.get("id") or link or e.get("title", "")
            body = e.get("summary") or ""
            if not body and e.get("content"):
                body = e["content"][0].get("value", "")
            if kind == "video":
                body = (e.get("media_description") or body)
            yield Item(
                source=self.name,
                native_id=f"{feed['name']}:{hashlib.sha1(uid.encode()).hexdigest()[:16]}",
                kind=kind,
                title=strip_html(e.get("title")) or "(untitled)",
                url=link,
                author=e.get("author"),
                text=truncate(strip_html(body), 2500),
                channel=feed["name"],
                created_at=created,
                tags=[t["term"].lower() for t in e.get("tags", []) if t.get("term")][:10],
                extra={"feed_group": feed.get("group")},
            )
