"""arXiv daily announcements via RSS (new + cross-listed papers only)."""

from __future__ import annotations

import re

import feedparser

from whats_cooking.collectors.base import Collector, parse_dt, register, strip_html, truncate
from whats_cooking.models import Item

_ID = re.compile(r"(\d{4}\.\d{4,5})")
_ABSTRACT = re.compile(r"^.*?Abstract:\s*", re.S)


@register
class Arxiv(Collector):
    name = "arxiv"

    def collect(self):
        cats = self.cfg.get("categories", ["cs.AI", "cs.LG", "cs.CL"])
        keep = set(self.cfg.get("announce_types", ["new", "cross"]))
        try:
            raw = self.http.get(f"https://rss.arxiv.org/rss/{'+'.join(cats)}").content
        except Exception as exc:
            self.fail("rss", exc)
            return
        seen: set[str] = set()
        for e in feedparser.parse(raw).entries:
            if e.get("arxiv_announce_type", "new") not in keep:
                continue
            m = _ID.search(e.get("id", "") or e.get("link", ""))
            if not m or m.group(1) in seen:
                continue
            arxiv_id = m.group(1)
            seen.add(arxiv_id)
            categories = [t["term"] for t in e.get("tags", []) if t.get("term")]
            yield Item(
                source=self.name,
                native_id=arxiv_id,
                kind="paper",
                title=strip_html(e.get("title")),
                url=f"https://arxiv.org/abs/{arxiv_id}",
                author=e.get("author"),
                text=truncate(_ABSTRACT.sub("", strip_html(e.get("summary"))), 2500),
                channel=categories[0] if categories else "",
                created_at=parse_dt(e.get("published_parsed") or e.get("published")),
                tags=categories,
            )
