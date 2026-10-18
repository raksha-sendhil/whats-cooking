"""Lobsters hottest + active pages (public JSON)."""

from __future__ import annotations

from whats_cooking.collectors.base import Collector, parse_dt, register, truncate
from whats_cooking.models import Item


@register
class Lobsters(Collector):
    name = "lobsters"

    def collect(self):
        min_score = self.cfg.get("min_score", 5)
        seen: set[str] = set()
        for path in self.cfg.get("pages", ["hottest.json", "page/2.json", "active.json"]):
            try:
                stories = self.http.get_json(f"https://lobste.rs/{path}")
            except Exception as exc:
                self.fail(path, exc)
                continue
            for s in stories:
                if s["short_id"] in seen or s.get("score", 0) < min_score:
                    continue
                seen.add(s["short_id"])
                yield Item(
                    source=self.name,
                    native_id=s["short_id"],
                    kind="story",
                    title=s["title"],
                    url=s.get("url") or s["comments_url"],
                    discussion_url=s["comments_url"],
                    author=s.get("submitter_user"),
                    text=truncate(s.get("description_plain")),
                    created_at=parse_dt(s.get("created_at")),
                    metrics={"score": s.get("score"), "comments": s.get("comment_count")},
                    tags=s.get("tags", []),
                )
