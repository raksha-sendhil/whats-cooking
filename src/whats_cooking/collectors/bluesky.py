"""Bluesky: posts (and reposts) from tracked researchers/builders, plus optional custom feeds.

Uses the public AppView, no login. (Search requires auth, so we follow accounts instead.)
"""

from __future__ import annotations

from whats_cooking.collectors.base import Collector, parse_dt, register, truncate
from whats_cooking.models import Item

API = "https://public.api.bsky.app/xrpc"


@register
class Bluesky(Collector):
    name = "bluesky"

    def collect(self):
        since = self.since(48)
        min_likes = self.cfg.get("min_likes", 20)
        seen: set[str] = set()
        accounts = list(dict.fromkeys(self.cfg.get("accounts", []) + self.ctx.watch.get("bluesky_account", [])))
        sources = [("author", h) for h in accounts]
        sources += [("feed", f) for f in self.cfg.get("feeds", [])]
        for kind, target in sources:
            try:
                if kind == "author":
                    data = self.http.get_json(f"{API}/app.bsky.feed.getAuthorFeed", params={
                        "actor": target, "limit": 50, "filter": "posts_no_replies",
                    })
                else:
                    data = self.http.get_json(f"{API}/app.bsky.feed.getFeed", params={"feed": target, "limit": 50})
            except Exception as exc:
                self.fail(target, exc)
                continue
            for entry in data.get("feed", []):
                post = entry["post"]
                created = parse_dt(post.get("record", {}).get("createdAt"))
                if post["uri"] in seen or not created or created < since:
                    continue
                if (post.get("likeCount") or 0) < min_likes:
                    continue
                seen.add(post["uri"])
                reason = entry.get("reason") or {}
                via = f"repost:{target}" if "Repost" in reason.get("$type", "") else target
                yield self._item(post, created, channel=via if kind == "author" else f"feed:{target.rsplit('/', 1)[-1]}")

    def _item(self, post: dict, created, channel: str) -> Item:
        record = post.get("record", {})
        text = record.get("text", "")
        handle = post["author"]["handle"]
        web_url = f"https://bsky.app/profile/{handle}/post/{post['uri'].rsplit('/', 1)[-1]}"
        external = _external(post.get("embed") or {})
        quoted = _quoted_author(post.get("embed") or {})
        links = [f["uri"] for facet in record.get("facets", []) for f in facet.get("features", []) if f.get("uri")]
        if external:
            title = external.get("title") or text[:140]
            body = f"{text}\n\n{external.get('title', '')}: {external.get('description', '')}".strip()
            url = external.get("uri")
        else:
            title = text.split("\n", 1)[0][:140] or "(Bluesky post)"
            body = text
            url = links[0] if len(links) == 1 else web_url
        return Item(
            source=self.name,
            native_id=post["uri"],
            kind="post",
            title=title,
            url=url,
            discussion_url=web_url,
            author=handle,
            text=truncate(body + ("\n" + "\n".join(links) if links else "")),
            channel=channel,
            created_at=created,
            metrics={
                "likes": post.get("likeCount"),
                "reposts": post.get("repostCount"),
                "replies": post.get("replyCount"),
                "quotes": post.get("quoteCount"),
            },
            extra={"quoted_author": quoted},
        )


def _quoted_author(embed: dict) -> str | None:
    """Handle of the author of a quoted post (embed.record or recordWithMedia.record.record)."""
    rec = embed.get("record") or {}
    if "record" in rec and isinstance(rec["record"], dict):   # recordWithMedia wraps one level deeper
        rec = rec["record"]
    author = rec.get("author") if isinstance(rec, dict) else None
    return author.get("handle") if isinstance(author, dict) else None


def _external(embed: dict) -> dict | None:
    if "external" in embed:
        return embed["external"]
    media = embed.get("media") or {}
    return media.get("external")
