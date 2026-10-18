"""Public Discourse forums (product/dev communities): today's top topics.

These are where users of specific tools complain and ask for features.
"""

from __future__ import annotations

import re

from whats_cooking.collectors.base import Collector, parse_dt, register, strip_html, truncate
from whats_cooking.models import Item

# Spam that slips into forum "top" lists (phone-number ads etc.).
_SPAM = re.compile(r"\d{9,}|call ?girl|escort|whatsapp|casino|betting|loan app|customer care", re.I)


@register
class Discourse(Collector):
    name = "discourse"

    def collect(self):
        min_activity = self.cfg.get("min_activity", 4)
        for host in self.cfg.get("forums", []):
            try:
                data = self.http.get_json(f"https://{host}/top.json", params={"period": self.cfg.get("period", "daily")})
            except Exception as exc:
                self.fail(host, exc)
                continue
            for t in data.get("topic_list", {}).get("topics", []):
                title = t.get("title") or ""
                activity = (t.get("like_count") or 0) + 2 * (t.get("reply_count") or 0)
                if t.get("pinned") or activity < min_activity or _SPAM.search(title):
                    continue
                url = f"https://{host}/t/{t.get('slug', 'topic')}/{t['id']}"
                yield Item(
                    source=self.name,
                    native_id=f"{host}:{t['id']}",
                    kind="thread",
                    title=title,
                    url=url,
                    discussion_url=url,
                    text=truncate(strip_html(t.get("excerpt"))),
                    channel=host,
                    created_at=parse_dt(t.get("created_at")),
                    metrics={
                        "likes": t.get("like_count"),
                        "replies": t.get("reply_count"),
                        "views": t.get("views"),
                    },
                    tags=[x if isinstance(x, str) else x.get("name", "") for x in t.get("tags", [])],
                )
