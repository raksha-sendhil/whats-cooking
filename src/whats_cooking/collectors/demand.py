"""Company demand: what companies and investors say they're trying to build or fix.

  hn_hiring  every company post in the latest "Ask HN: Who is hiring?" thread (monthly)
  yc_rfs     Y Combinator's Requests for Startups
These feed problem mining only; they never become news topics.
"""

from __future__ import annotations

import re

from selectolax.lexbor import LexborHTMLParser

from whats_cooking.collectors.base import Collector, parse_dt, register, strip_html, truncate
from whats_cooking.models import Item


@register
class HNHiring(Collector):
    name = "hn_hiring"

    def collect(self):
        try:
            hits = self.http.get_json("https://hn.algolia.com/api/v1/search_by_date", params={
                "tags": "story,author_whoishiring", "query": "Who is hiring", "hitsPerPage": 5})["hits"]
            thread = next(h for h in hits if h["title"].lower().startswith("ask hn: who is hiring"))
            data = self.http.get_json(f"https://hn.algolia.com/api/v1/items/{thread['objectID']}")
        except Exception as exc:
            self.fail("thread", exc)
            return
        month = re.sub(r".*\((.*)\).*", r"\1", thread["title"])
        for c in data.get("children", []):
            text = strip_html(c.get("text"))
            if len(text) < 80 or text.lower().startswith("location:"):   # job seekers posting in the wrong thread
                continue
            company = text.split("|", 1)[0].strip()[:80]
            yield Item(
                source=self.name,
                native_id=str(c["id"]),
                kind="job",
                title=company or "(hiring post)",
                url=f"https://news.ycombinator.com/item?id={c['id']}",
                discussion_url=f"https://news.ycombinator.com/item?id={c['id']}",
                author=c.get("author"),
                text=truncate(text, 1500),
                channel=month,
                created_at=parse_dt(c.get("created_at_i") or c.get("created_at")),
            )


@register
class YCRequests(Collector):
    name = "yc_rfs"

    def collect(self):
        url = "https://www.ycombinator.com/rfs"
        try:
            tree = LexborHTMLParser(self.http.get_text(url))
        except Exception as exc:
            self.fail("rfs", exc)
            return
        for h in tree.css("h3"):
            title = h.text(strip=True)
            block = h.parent.parent if h.parent is not None and h.parent.parent is not None else None
            body = block.text(separator=" ", strip=True) if block else ""
            body = re.sub(r"\s+", " ", body)
            # Real requests start with their heading followed by an author byline ("The Primer By Andrew
            # Miklas ..."); nav/footer headings don't.
            if len(title) < 4 or len(body) < 200 or not body.startswith(title) \
                    or not body[len(title):].lstrip().startswith("By "):
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            yield Item(
                source=self.name,
                native_id=slug,
                kind="request",
                title=title,
                url=f"{url}#{slug}",
                text=truncate(body, 2500),
                channel="rfs",
            )
