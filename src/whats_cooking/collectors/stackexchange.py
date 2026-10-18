"""Stack Exchange: hot questions on AI/dev sites and Stack Overflow tags.

300 requests/day per IP without a key; set STACKEXCHANGE_KEY for 10k/day.
"""

from __future__ import annotations

import html
import os
import time

from whats_cooking.collectors.base import Collector, parse_dt, register, strip_html, truncate
from whats_cooking.models import Item

API = "https://api.stackexchange.com/2.3/questions"


@register
class StackExchange(Collector):
    name = "stackexchange"

    def collect(self):
        key = os.environ.get("STACKEXCHANGE_KEY")
        min_score = self.cfg.get("min_score", 3)
        seen: set[str] = set()
        for entry in self.cfg.get("queries", []):
            site, tag = entry["site"], entry.get("tag")
            params = {"site": site, "sort": entry.get("sort", "hot"), "order": "desc", "pagesize": 30, "filter": "withbody"}
            if tag:
                params["tagged"] = tag
            if key:
                params["key"] = key
            try:
                data = self.http.get_json(API, params=params)
            except Exception as exc:
                self.fail(f"{site}/{tag or '*'}", exc)
                continue
            for q in data.get("items", []):
                qid = f"{site}:{q['question_id']}"
                if qid in seen or q.get("score", 0) < min_score:
                    continue
                seen.add(qid)
                yield Item(
                    source=self.name,
                    native_id=qid,
                    kind="question",
                    title=html.unescape(q["title"]),
                    url=q["link"],
                    discussion_url=q["link"],
                    author=(q.get("owner") or {}).get("display_name"),
                    text=truncate(strip_html(q.get("body")), 1500),
                    channel=f"{site}/{tag}" if tag else site,
                    created_at=parse_dt(q.get("creation_date")),
                    metrics={
                        "score": q.get("score"),
                        "answers": q.get("answer_count"),
                        "views": q.get("view_count"),
                        "answered": 1 if q.get("is_answered") else 0,
                    },
                    tags=q.get("tags", []),
                )
            if data.get("backoff"):  # API-mandated pause
                time.sleep(min(int(data["backoff"]), 30))
