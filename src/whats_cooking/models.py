"""Common item format every collector emits."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# What an item *is*. Used for grouping and, later, classification.
KINDS = {
    "story",      # link post on a news aggregator (HN, Lobsters, Reddit)
    "show",       # Show HN / launch-style post by the builder
    "ask",        # Ask HN / question thread
    "paper",
    "repo",
    "release",
    "issue",      # GitHub issue (often a feature request = pain point)
    "model",
    "space",
    "dataset",
    "post",       # social post (Bluesky, Mastodon)
    "link",       # trending link (Mastodon trends)
    "article",    # blog / newsletter / news article
    "video",
    "podcast",
    "question",   # Stack Exchange
    "thread",     # forum topic (Discourse)
    "launch",     # Product Hunt
    "job",        # a company's "Who is hiring" post: what they're building (problem-mining only)
    "request",    # an investor's request for startups (problem-mining only)
}

# Kinds that only feed problem mining, never news topics.
DEMAND_KINDS = {"job", "request"}


@dataclass
class Item:
    source: str
    native_id: str
    kind: str
    title: str
    url: str | None = None
    discussion_url: str | None = None
    author: str | None = None
    text: str = ""
    channel: str = ""
    created_at: datetime | None = None
    metrics: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return f"{self.source}:{self.native_id}"

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind!r} for {self.source}")
