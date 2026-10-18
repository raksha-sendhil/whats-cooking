"""Hugging Face: community-upvoted daily papers + trending models, Spaces, datasets."""

from __future__ import annotations

from whats_cooking.collectors.base import Collector, parse_dt, register, truncate
from whats_cooking.models import Item

API = "https://huggingface.co/api"


@register
class HuggingFace(Collector):
    name = "huggingface"

    def collect(self):
        if self.cfg.get("daily_papers", True):
            try:
                yield from self._daily_papers()
            except Exception as exc:
                self.fail("daily_papers", exc)
        for kind in ("models", "spaces", "datasets"):
            limit = self.cfg.get(f"trending_{kind}", 50)
            if not limit:
                continue
            try:
                yield from self._trending(kind, limit)
            except Exception as exc:
                self.fail(f"trending_{kind}", exc)

    def _daily_papers(self):
        for p in self.http.get_json(f"{API}/daily_papers", params={"limit": self.cfg.get("papers_limit", 100)}):
            paper = p["paper"]
            arxiv_id = paper["id"]
            yield Item(
                source=self.name,
                native_id=f"paper:{arxiv_id}",
                kind="paper",
                title=paper.get("title") or p.get("title", ""),
                url=f"https://huggingface.co/papers/{arxiv_id}",
                text=truncate(paper.get("summary"), 2500),
                channel="daily_papers",
                created_at=parse_dt(paper.get("publishedAt")),
                metrics={"upvotes": paper.get("upvotes"), "comments": p.get("numComments")},
                extra={
                    "organization": (paper.get("organization") or {}).get("fullname")
                    if isinstance(paper.get("organization"), dict) else None,
                    "primary_refs": [f"arxiv:{arxiv_id}"],
                },
            )

    def _trending(self, kind: str, limit: int):
        singular = kind.rstrip("s")
        for m in self.http.get_json(f"{API}/{kind}", params={"sort": "trendingScore", "limit": limit}):
            repo_id = m["id"]
            prefix = "" if kind == "models" else f"{kind}/"
            tags = [t for t in m.get("tags", []) if ":" not in t][:20]
            if m.get("pipeline_tag"):
                tags.append(m["pipeline_tag"])
            yield Item(
                source=self.name,
                native_id=f"{singular}:{repo_id.lower()}",
                kind=singular,
                title=repo_id,
                url=f"https://huggingface.co/{prefix}{repo_id}",
                author=repo_id.split("/")[0] if "/" in repo_id else None,
                text=truncate(" ".join(tags)),
                channel=f"trending_{kind}",
                created_at=parse_dt(m.get("createdAt")),
                metrics={
                    "likes": m.get("likes"),
                    "trending_score": m.get("trendingScore"),
                    "downloads": m.get("downloads"),
                },
                tags=tags,
                extra={"base_models": [t.split(":", 2)[-1] for t in m.get("tags", []) if t.startswith("base_model:") and t.count(":") >= 2][:5] or None},
            )
