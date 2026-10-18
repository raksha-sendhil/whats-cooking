"""Watch news/blog index pages that have no RSS (Anthropic, Meta AI...).

Every link matching the page's pattern becomes an item; "new" = first time we see it.
On the very first run of a page, everything is marked as baseline so old posts don't
look like fresh news.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from whats_cooking.collectors.base import Collector, register
from whats_cooking.models import Item
from whats_cooking.refs import canonical_url

_MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_DATE = re.compile(rf"\b{_MONTHS}\s+\d{{1,2}},?\s+\d{{4}}\b")
_BY = re.compile(r"\s+By\s+[A-Z][\w .,'-]*$")
_CATEGORY = re.compile(
    r"^(?:(?:Announcements?|Science|Research|Company|Product|Products|Solutions|Engineering|Policy|"
    r"Interpretability|Societal Impacts|Featured|Case Study|News|Alignment|Economic Research|Event)\s+)+",
    re.I,
)
_HEADING = "h1, h2, h3, h4, h5, [class*=title], [class*=Title], [class*=headline]"


@register
class Pages(Collector):
    name = "pages"

    def collect(self):
        for page in self.cfg.get("pages", []):
            try:
                html = self.http.get_text(page["url"])
            except Exception as exc:
                self.fail(page["name"], exc)
                continue
            first_run = (self.name, page["name"]) not in self.ctx.known_channels
            pattern = re.compile(page["pattern"])
            page_canon = canonical_url(page["url"])
            titles: dict[str, tuple[str, str]] = {}  # canonical url -> (href, best title)
            for a in LexborHTMLParser(html).css("a[href]"):
                href = urljoin(page["url"], a.attributes.get("href") or "")
                canon = canonical_url(href)
                if not canon or canon == page_canon or not pattern.search(href):
                    continue
                title = link_title(a)
                if _better(title, titles.get(canon, ("", ""))[1]):
                    titles[canon] = (href, title)
            for canon, (href, title) in titles.items():
                if len(title) < 8:
                    continue
                yield Item(
                    source=self.name,
                    native_id=canon,
                    kind=page.get("kind", "article"),
                    title=title,
                    url=href,
                    channel=page["name"],
                    extra={"baseline": True} if first_run else {},
                )


def link_title(a) -> str:
    """Best-guess headline for a card link: a heading inside it, else its cleaned text."""
    heading = a.css_first(_HEADING)
    text = heading.text(separator=" ", strip=True) if heading else a.text(separator=" ", strip=True)
    return clean_title(text)


def clean_title(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = _DATE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = _CATEGORY.sub("", s)
    s = _BY.sub("", s)
    s = re.sub(r"\b(read more|learn more)\b.*$", "", s, flags=re.I).strip()
    return s[:200]


def _better(new: str, old: str) -> bool:
    def score(t: str) -> int:
        if len(t) < 8 or (t.isupper() and " " not in t):
            return 0
        return min(len(t), 160)
    return score(new) > score(old)
