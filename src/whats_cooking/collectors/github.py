"""GitHub: fast-rising new repos, trending page, releases of key repos, most-wanted issues.

Works without a token (60 core / 10 search requests per hour); set GITHUB_TOKEN for 5000/hour.
"""

from __future__ import annotations

from datetime import timedelta

from selectolax.lexbor import LexborHTMLParser

from whats_cooking.collectors.base import Collector, as_int, parse_dt, register, truncate
from whats_cooking.models import Item

API = "https://api.github.com"
JSON = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


@register
class GitHub(Collector):
    name = "github"

    def collect(self):
        steps = [
            ("new_repos", self._new_repos),
            ("trending", self._trending),
            ("releases", self._releases),
            ("hot_issues", self._hot_issues),
        ]
        for label, step in steps:
            if not self.cfg.get(label, {}).get("enabled", True):
                continue
            try:
                yield from step(self.cfg.get(label, {}))
            except Exception as exc:
                self.fail(label, exc)

    # -- new repos gaining stars fast ------------------------------------------

    def _new_repos(self, cfg: dict):
        created_after = (self.ctx.now - timedelta(days=cfg.get("created_within_days", 7))).date().isoformat()
        q = f"created:>{created_after} stars:>={cfg.get('min_stars', 50)}"
        for page in range(1, cfg.get("pages", 3) + 1):
            data = self.http.get_json(f"{API}/search/repositories", headers=JSON, params={
                "q": q, "sort": "stars", "order": "desc", "per_page": 100, "page": page,
            })
            for r in data.get("items", []):
                yield self._repo_item(r, channel="new_repos")
            if len(data.get("items", [])) < 100:
                return

    def _repo_item(self, r: dict, channel: str) -> Item:
        return Item(
            source=self.name,
            native_id=r["full_name"].lower(),
            kind="repo",
            title=f"{r['full_name']}: {r.get('description') or ''}".strip().rstrip(":"),
            url=r["html_url"],
            author=r.get("owner", {}).get("login"),
            text=truncate(r.get("description")),
            channel=channel,
            created_at=parse_dt(r.get("created_at")),
            metrics={
                "stars": r.get("stargazers_count"),
                "forks": r.get("forks_count"),
                "open_issues": r.get("open_issues_count"),
            },
            tags=[*(r.get("topics") or []), *([r["language"].lower()] if r.get("language") else [])],
        )

    # -- github.com/trending (HTML; no API exists) -----------------------------

    def _trending(self, cfg: dict):
        for lang in cfg.get("languages", [""]):
            url = f"https://github.com/trending/{lang}".rstrip("/")
            try:
                html = self.http.get_text(url, params={"since": cfg.get("since", "daily")})
            except Exception as exc:
                self.fail(f"trending/{lang}", exc)
                continue
            for rank, row in enumerate(LexborHTMLParser(html).css("article.Box-row"), start=1):
                link = row.css_first("h2 a")
                if not link:
                    continue
                full_name = (link.attributes.get("href") or "").strip("/")
                if full_name.count("/") != 1:
                    continue
                desc_node = row.css_first("p")
                desc = desc_node.text(strip=True) if desc_node else ""
                today = row.css_first("span.d-inline-block.float-sm-right")
                stars = row.css_first("a[href$='/stargazers']")
                forks = row.css_first("a[href$='/forks']")
                lang_node = row.css_first("[itemprop=programmingLanguage]")
                yield Item(
                    source=self.name,
                    native_id=full_name.lower(),
                    kind="repo",
                    title=f"{full_name}: {desc}".rstrip(": "),
                    url=f"https://github.com/{full_name}",
                    author=full_name.split("/")[0],
                    text=truncate(desc),
                    channel="trending",
                    metrics={
                        "stars": as_int(stars.text()) if stars else None,
                        "forks": as_int(forks.text()) if forks else None,
                        "stars_today": as_int(today.text()) if today else None,
                    },
                    tags=[lang_node.text(strip=True).lower()] if lang_node else [],
                    extra={f"trending_rank_{lang or 'all'}": rank},
                )

    # -- releases of repos we care about ----------------------------------------

    def _releases(self, cfg: dict):
        since = self.since(cfg.get("lookback_hours", 48))
        static = list(cfg.get("repos", []))
        dynamic = [r for r in self.ctx.watch.get("github_repo", []) if r.lower() not in {s.lower() for s in static}]
        for repo in static + dynamic:
            try:
                releases = self.http.get_json(f"{API}/repos/{repo}/releases", headers=JSON, params={"per_page": 5})
            except Exception as exc:
                self.fail(f"releases/{repo}", exc)
                continue
            for rel in releases:
                published = parse_dt(rel.get("published_at"))
                if rel.get("draft") or not published or published < since:
                    continue
                if rel.get("prerelease") and not cfg.get("include_prereleases", False):
                    continue
                name = rel.get("name") or rel.get("tag_name")
                yield Item(
                    source=self.name,
                    native_id=f"{repo.lower()}@{rel['tag_name']}",
                    kind="release",
                    title=f"{repo} {name}",
                    url=rel["html_url"],
                    author=(rel.get("author") or {}).get("login"),
                    text=truncate(rel.get("body"), 3000),
                    channel=repo.lower(),
                    created_at=published,
                    metrics={"reactions": (rel.get("reactions") or {}).get("total_count")},
                    extra={"refs": [f"github:{repo.lower()}"]},
                )

    # -- most-upvoted recent issues: ranked lists of unsolved problems --------

    def _hot_issues(self, cfg: dict):
        created_after = (self.ctx.now - timedelta(days=cfg.get("created_within_days", 30))).date().isoformat()
        q = f"is:issue is:open created:>{created_after} reactions:>={cfg.get('min_reactions', 25)}"
        data = self.http.get_json(f"{API}/search/issues", headers=JSON, params={
            "q": q, "sort": "reactions-+1", "order": "desc", "per_page": 100,
        })
        for i in data.get("items", []):
            repo = i["repository_url"].split("/repos/")[-1].lower()
            reactions = i.get("reactions") or {}
            yield Item(
                source=self.name,
                native_id=f"{repo}#{i['number']}",
                kind="issue",
                title=f"[{repo}] {i['title']}",
                url=i["html_url"],
                author=(i.get("user") or {}).get("login"),
                text=truncate(i.get("body"), 3000),
                channel=repo,
                created_at=parse_dt(i.get("created_at")),
                metrics={
                    "thumbs_up": reactions.get("+1"),
                    "reactions": reactions.get("total_count"),
                    "comments": i.get("comments"),
                },
                tags=[lbl["name"].lower() for lbl in i.get("labels", []) if isinstance(lbl, dict)],
                extra={"refs": [f"github:{repo}"]},
            )
