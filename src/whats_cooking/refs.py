"""URL canonicalization and reference extraction.

A "ref" is a stable identifier for the thing an item is about:
  arxiv:2610.01234, github:owner/repo, hf:owner/name, youtube:VIDEOID, url:host/path
Items that share a ref are about the same thing, regardless of source.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = re.compile(r"^(utm_.*|ref|ref_src|source|fbclid|gclid|mc_cid|mc_eid|s|si|cmpid|share)$", re.I)

_ARXIV_ID = r"(\d{4}\.\d{4,5})(?:v\d+)?"
_ARXIV_URL = re.compile(rf"(?:arxiv\.org/(?:abs|pdf|html)/|alphaxiv\.org/(?:abs/|overview/)?|huggingface\.co/papers/){_ARXIV_ID}", re.I)
_ARXIV_TEXT = re.compile(rf"\barXiv:\s?{_ARXIV_ID}", re.I)
_GITHUB = re.compile(r"github\.com/([A-Za-z0-9][\w.-]*)/([\w.-]+)(?:/(issues|pull|discussions)/(\d+)|/releases/tag/([^/?#\s]+))?", re.I)
_HF = re.compile(r"huggingface\.co/(?:(spaces|datasets)/)?([\w.-]+)/([\w.-]+)", re.I)
_YOUTUBE = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/)|youtu\.be/)([\w-]{11})", re.I)
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')\]]+")

# github.com/<x>/... paths that are not repositories
_GITHUB_NON_REPO = {
    "features", "orgs", "sponsors", "topics", "trending", "collections", "marketplace",
    "settings", "notifications", "login", "about", "pricing", "enterprise", "security",
    "apps", "site", "readme", "events", "explore", "users", "search",
}
_HF_NON_REPO = {"docs", "blog", "papers", "learn", "posts", "pricing", "settings", "join", "login", "tasks", "models", "spaces", "datasets", "collections", "organizations"}


def canonical_url(url: str | None) -> str | None:
    """Normalize a URL so trivially different links compare equal."""
    if not url:
        return None
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        return None
    parts = urlsplit(url)
    host = parts.hostname or ""
    host = host.lower().removeprefix("www.").removeprefix("m.").removeprefix("mobile.")
    if host == "old.reddit.com":
        host = "reddit.com"
    path = re.sub(r"/+", "/", parts.path).rstrip("/") or "/"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if not _TRACKING_PARAMS.match(k)]
    query.sort()
    return urlunsplit(("https", host, path, urlencode(query), ""))


def url_ref(url: str | None) -> str | None:
    """Most specific ref for a single URL, or None."""
    if not url:
        return None
    if m := _ARXIV_URL.search(url):
        return f"arxiv:{m.group(1)}"
    if m := _YOUTUBE.search(url):
        return f"youtube:{m.group(1)}"
    if m := _GITHUB.search(url):
        owner, repo = m.group(1).lower(), m.group(2).lower().removesuffix(".git")
        if owner not in _GITHUB_NON_REPO and repo:
            # An issue/PR/release is its own thing, not the repo.
            if m.group(4):
                return f"github:{owner}/{repo}#{m.group(4)}"
            if m.group(5):
                return f"github:{owner}/{repo}@{m.group(5)}"
            return f"github:{owner}/{repo}"
    if m := _HF.search(url):
        kind, owner, name = m.group(1), m.group(2).lower(), m.group(3).lower()
        if owner not in _HF_NON_REPO:
            prefix = f"{kind}/" if kind else ""
            return f"hf:{prefix}{owner}/{name}"
    canon = canonical_url(url)
    if canon is None:
        return None
    host_path = canon.removeprefix("https://")
    # Bare homepages are too generic to identify a single thing.
    if host_path.endswith("/") or "/" not in host_path:
        return None
    return f"url:{host_path}"


def refs_from_text(text: str | None) -> set[str]:
    """Refs mentioned anywhere in free text (links + bare arXiv ids)."""
    if not text:
        return set()
    refs = {f"arxiv:{m.group(1)}" for m in _ARXIV_TEXT.finditer(text)}
    for u in _URL_IN_TEXT.findall(text):
        if r := url_ref(u.rstrip(".,;:")):
            refs.add(r)
    return refs
