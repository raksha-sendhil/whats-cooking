"""Importing this package registers every collector in REGISTRY."""

from whats_cooking.collectors import (  # noqa: F401
    arxiv,
    bluesky,
    demand,
    devto,
    discourse,
    github,
    hackernews,
    huggingface,
    lobsters,
    mastodon,
    pages,
    producthunt,
    reddit,
    rss,
    stackexchange,
)
from whats_cooking.collectors.base import REGISTRY, Collector, Context

__all__ = ["REGISTRY", "Collector", "Context"]
