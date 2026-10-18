"""Hacker News via the Algolia search API (free, no key)."""

from __future__ import annotations

from whats_cooking.collectors.base import Collector, parse_dt, register, strip_html, truncate
from whats_cooking.models import Item

API = "https://hn.algolia.com/api/v1/search_by_date"


@register
class HackerNews(Collector):
    name = "hackernews"

    def collect(self):
        since = int(self.since(36).timestamp())
        front_page = self._front_page_ids()
        queries = [
            ("story", self.cfg.get("min_points", 30)),
            ("show_hn", self.cfg.get("min_points_show", 10)),
            ("ask_hn", self.cfg.get("min_points_ask", 10)),
        ]
        seen: set[str] = set()
        for tag, min_points in queries:
            try:
                for hit in self._search(tag, since, min_points):
                    if hit["objectID"] in seen:
                        continue
                    seen.add(hit["objectID"])
                    yield self._item(hit, hit["objectID"] in front_page)
            except Exception as exc:
                self.fail(tag, exc)

    def _front_page_ids(self) -> set[str]:
        try:
            data = self.http.get_json("https://hn.algolia.com/api/v1/search", params={"tags": "front_page", "hitsPerPage": 100})
            return {h["objectID"] for h in data["hits"]}
        except Exception as exc:
            self.fail("front_page", exc)
            return set()

    def _search(self, tag: str, since: int, min_points: int):
        page = 0
        while True:
            data = self.http.get_json(API, params={
                "tags": tag,
                "numericFilters": f"created_at_i>{since},points>={min_points}",
                "hitsPerPage": 500,
                "page": page,
            })
            yield from data["hits"]
            page += 1
            if page >= data.get("nbPages", 0):
                return

    def _item(self, h: dict, on_front_page: bool) -> Item:
        tags = h.get("_tags", [])
        title = h.get("title") or ""
        if "show_hn" in tags:
            kind = "show"
        elif "ask_hn" in tags:
            kind = "ask"
        elif title.lower().startswith("launch hn"):
            kind = "show"
        else:
            kind = "story"
        discussion = f"https://news.ycombinator.com/item?id={h['objectID']}"
        return Item(
            source=self.name,
            native_id=h["objectID"],
            kind=kind,
            title=title,
            url=h.get("url") or discussion,
            discussion_url=discussion,
            author=h.get("author"),
            text=truncate(strip_html(h.get("story_text"))),
            channel=kind,
            created_at=parse_dt(h.get("created_at_i")),
            metrics={
                "points": h.get("points"),
                "comments": h.get("num_comments"),
                "front_page": 1 if on_front_page else None,
            },
        )
