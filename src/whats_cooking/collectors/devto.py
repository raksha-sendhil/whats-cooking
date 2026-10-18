"""dev.to: top articles of the last day per tag."""

from __future__ import annotations

from whats_cooking.collectors.base import Collector, parse_dt, register, truncate
from whats_cooking.models import Item


@register
class DevTo(Collector):
    name = "devto"

    def collect(self):
        min_reactions = self.cfg.get("min_reactions", 15)
        seen: set[int] = set()
        for tag in self.cfg.get("tags", []):
            try:
                articles = self.http.get_json("https://dev.to/api/articles", params={"top": 1, "tag": tag, "per_page": 30})
            except Exception as exc:
                self.fail(tag, exc)
                continue
            for a in articles:
                if a["id"] in seen or (a.get("positive_reactions_count") or 0) < min_reactions:
                    continue
                seen.add(a["id"])
                yield Item(
                    source=self.name,
                    native_id=str(a["id"]),
                    kind="article",
                    title=a["title"],
                    url=a["url"],
                    author=(a.get("user") or {}).get("username"),
                    text=truncate(a.get("description")),
                    channel=tag,
                    created_at=parse_dt(a.get("published_timestamp") or a.get("published_at")),
                    metrics={"reactions": a.get("positive_reactions_count"), "comments": a.get("comments_count")},
                    tags=a.get("tag_list") or [],
                )
