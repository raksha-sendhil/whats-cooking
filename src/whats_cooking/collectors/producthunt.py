"""Product Hunt launches.

With PRODUCTHUNT_TOKEN (free developer token) uses the GraphQL API and gets vote counts;
otherwise falls back to the public feed (titles only, no votes).
"""

from __future__ import annotations

import os

import feedparser

from whats_cooking.collectors.base import Collector, parse_dt, register, strip_html, truncate
from whats_cooking.models import Item

GRAPHQL = "https://api.producthunt.com/v2/api/graphql"
QUERY = """
query($after: DateTime!, $n: Int!) {
  posts(order: VOTES, postedAfter: $after, first: $n) {
    edges { node {
      id name tagline description url website votesCount commentsCount createdAt
      topics(first: 5) { edges { node { name } } }
    } }
  }
}
"""


@register
class ProductHunt(Collector):
    name = "producthunt"

    def collect(self):
        token = os.environ.get("PRODUCTHUNT_TOKEN")
        try:
            if token:
                yield from self._graphql(token)
            else:
                yield from self._feed()
        except Exception as exc:
            self.fail("graphql" if token else "feed", exc)

    def _graphql(self, token: str):
        data = self.http.post_json(
            GRAPHQL,
            {"query": QUERY, "variables": {"after": self.since(36).isoformat(), "n": self.cfg.get("limit", 50)}},
            headers={"Authorization": f"Bearer {token}"},
        )
        min_votes = self.cfg.get("min_votes", 50)
        for edge in data["data"]["posts"]["edges"]:
            p = edge["node"]
            if (p.get("votesCount") or 0) < min_votes:
                continue
            yield Item(
                source=self.name,
                native_id=str(p["id"]),
                kind="launch",
                title=f"{p['name']}: {p.get('tagline', '')}",
                url=p.get("website") or p["url"],
                discussion_url=p["url"],
                text=truncate(p.get("description")),
                created_at=parse_dt(p.get("createdAt")),
                metrics={"votes": p.get("votesCount"), "comments": p.get("commentsCount")},
                tags=[t["node"]["name"].lower() for t in p.get("topics", {}).get("edges", [])],
            )

    def _feed(self):
        since = self.since(36)
        for e in feedparser.parse(self.http.get("https://www.producthunt.com/feed").content).entries:
            created = parse_dt(e.get("published_parsed") or e.get("published"))
            if created and created < since:
                continue
            yield Item(
                source=self.name,
                native_id=e.get("id") or e["link"],
                kind="launch",
                title=e.get("title", ""),
                url=e.get("link"),
                discussion_url=e.get("link"),
                text=truncate(strip_html(e.get("summary"))),
                created_at=created,
                extra={"no_votes": True},
            )
