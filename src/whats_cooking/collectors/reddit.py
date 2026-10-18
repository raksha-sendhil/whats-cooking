"""Reddit via public RSS (top of the day per subreddit).

Reddit ends RSS support on 2026-11-13, so this collector has a `sunset` in config and
stops on its own. RSS has no vote counts; the rank within the day's top list is the signal.
"""

from __future__ import annotations

import re
import time

import feedparser

from whats_cooking.collectors.base import Collector, parse_dt, register, strip_html, truncate
from whats_cooking.http import HttpError
from whats_cooking.models import Item

_EXTERNAL_LINK = re.compile(r'<a href="([^"]+)">\[link\]</a>')


@register
class Reddit(Collector):
    name = "reddit"

    def collect(self):
        limit = self.cfg.get("per_subreddit", 25)
        budget = self.cfg.get("time_budget_seconds", 240)
        cooldown = self.cfg.get("cooldown_seconds", 45)
        started = time.monotonic()
        subs = list(self.cfg.get("subreddits", []))
        for n, sub in enumerate(subs):
            if time.monotonic() - started > budget:
                self.fail("budget", TimeoutError(f"time budget hit; skipped {', '.join(subs[n:])}"))
                return
            raw = self._fetch(sub, limit, cooldown)
            if raw is None:
                if self.blocked:
                    self.fail("blocked", RuntimeError(f"Reddit refused this IP (403); skipped {', '.join(subs[n + 1:])}"))
                    return
                continue
            for rank, e in enumerate(feedparser.parse(raw).entries, start=1):
                content = (e.get("content") or [{}])[0].get("value", "") or e.get("summary", "")
                m = _EXTERNAL_LINK.search(content)
                external = m.group(1) if m and "reddit.com" not in m.group(1) else None
                yield Item(
                    source=self.name,
                    native_id=e.get("id", e.get("link")),
                    kind="story",
                    title=strip_html(e.get("title")),
                    url=external or e.get("link"),
                    discussion_url=e.get("link"),
                    author=(e.get("author") or "").removeprefix("/u/"),
                    text=truncate(re.sub(r"submitted by .*$", "", strip_html(content)).strip()),
                    channel=f"r/{sub}",
                    created_at=parse_dt(e.get("published_parsed") or e.get("updated_parsed")),
                    metrics={"day_rank": rank},
                )

    blocked = False

    def _fetch(self, sub: str, limit: int, cooldown: float) -> bytes | None:
        """One request; on a 429, wait out Reddit's window once instead of hammering it."""
        url = f"https://www.reddit.com/r/{sub}/top/.rss"
        params = {"t": "day", "limit": limit}
        for attempt in range(2):
            try:
                return self.http.get(url, params=params, retries=0).content
            except HttpError as exc:
                if exc.status == 429 and attempt == 0:
                    time.sleep(cooldown)
                    continue
                if exc.status == 403:   # Reddit blocks many cloud/datacenter IPs outright
                    self.blocked = True
                    return None
                self.fail(sub, exc)
            except Exception as exc:
                self.fail(sub, exc)
            return None
        return None
