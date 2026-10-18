"""Mastodon: trending links on tech-heavy instances + popular posts under tech hashtags."""

from __future__ import annotations

from whats_cooking.collectors.base import Collector, as_int, parse_dt, register, strip_html, truncate
from whats_cooking.models import Item
from whats_cooking.refs import canonical_url


@register
class Mastodon(Collector):
    name = "mastodon"

    def collect(self):
        for inst in self.cfg.get("trend_instances", []):
            try:
                yield from self._trending_links(inst)
            except Exception as exc:
                self.fail(f"{inst}/trends", exc)
        since = self.since(48)
        min_engagement = self.cfg.get("min_engagement", 15)
        seen: set[str] = set()
        for inst in self.cfg.get("tag_instances", []):
            for tag in self.cfg.get("tags", []):
                try:
                    statuses = self.http.get_json(f"https://{inst}/api/v1/timelines/tag/{tag}", params={"limit": 40})
                except Exception as exc:
                    self.fail(f"{inst}/#{tag}", exc)
                    continue
                for s in statuses:
                    created = parse_dt(s.get("created_at"))
                    engagement = (s.get("favourites_count") or 0) + 2 * (s.get("reblogs_count") or 0)
                    if s["uri"] in seen or not created or created < since or engagement < min_engagement:
                        continue
                    seen.add(s["uri"])
                    yield self._status(s, created, f"#{tag}")

    def _trending_links(self, inst: str):
        for link in self.http.get_json(f"https://{inst}/api/v1/trends/links", params={"limit": 20}):
            history = link.get("history") or []
            uses = sum(as_int(h.get("uses")) or 0 for h in history[:2])
            accounts = sum(as_int(h.get("accounts")) or 0 for h in history[:2])
            yield Item(
                source=self.name,
                native_id=f"link:{canonical_url(link['url']) or link['url']}",
                kind="link",
                title=link.get("title") or link["url"],
                url=link["url"],
                author=link.get("author_name") or link.get("provider_name"),
                text=truncate(link.get("description")),
                channel=f"trends:{inst}",
                created_at=parse_dt(link.get("published_at")),
                metrics={"shares_2d": uses, "accounts_2d": accounts},
            )

    def _status(self, s: dict, created, channel: str) -> Item:
        text = strip_html(s.get("content"))
        card = s.get("card") or {}
        return Item(
            source=self.name,
            native_id=f"status:{s['uri']}",
            kind="post",
            title=card.get("title") or text[:140] or "(Mastodon post)",
            url=card.get("url") or s.get("url"),
            discussion_url=s.get("url"),
            author=(s.get("account") or {}).get("acct"),
            text=truncate(f"{text}\n\n{card.get('description', '')}".strip()),
            channel=channel,
            created_at=created,
            metrics={
                "favourites": s.get("favourites_count"),
                "reblogs": s.get("reblogs_count"),
                "replies": s.get("replies_count"),
            },
            tags=[t["name"].lower() for t in s.get("tags", [])],
        )
