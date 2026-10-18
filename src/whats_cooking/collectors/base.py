"""Collector base class, registry and parsing helpers."""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime

from whats_cooking.http import Fetcher
from whats_cooking.models import Item

REGISTRY: dict[str, type[Collector]] = {}


def register(cls: type[Collector]) -> type[Collector]:
    REGISTRY[cls.name] = cls
    return cls


@dataclass
class Context:
    http: Fetcher
    known_channels: frozenset[tuple[str, str]] = frozenset()
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    watch: dict[str, list[str]] = field(default_factory=dict)   # dynamic watch lists from the DB


class Collector:
    """Subclasses set `name` and implement `collect()`.

    A collector should yield what it can and record per-feed failures in
    `self.errors` instead of raising, so one dead feed doesn't lose the rest.
    """

    name: str = ""

    def __init__(self, cfg: dict, ctx: Context):
        self.cfg = cfg
        self.ctx = ctx
        self.http = ctx.http
        self.errors: list[str] = []
        self.log = logging.getLogger(f"whats_cooking.collectors.{self.name}")

    def collect(self) -> Iterator[Item]:
        raise NotImplementedError

    def skip_reason(self) -> str | None:
        """Return a reason to skip this collector entirely (e.g. past its sunset date)."""
        sunset = self.cfg.get("sunset")
        if sunset and self.ctx.now.date() >= _as_date(sunset):
            return f"past sunset date {sunset}"
        return None

    def since(self, default_hours: float = 36) -> datetime:
        return self.ctx.now - timedelta(hours=float(self.cfg.get("lookback_hours", default_hours)))

    def fail(self, where: str, exc: Exception) -> None:
        msg = f"{where}: {type(exc).__name__}: {exc}"
        self.errors.append(msg[:500])
        self.log.warning(msg[:300])


def _as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


# ---- parsing helpers --------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<(br|/p|/div|/li)\s*/?>", "\n", s, flags=re.I)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    return _WS.sub(" ", s).strip()


def truncate(s: str | None, n: int = 2000) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def parse_dt(v) -> datetime | None:
    """Parse ISO-8601, RFC-2822, unix seconds, or a feedparser struct_time."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, UTC)
    if hasattr(v, "tm_year"):  # time.struct_time from feedparser (UTC)
        return datetime(*v[:6], tzinfo=UTC)
    s = str(v).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def as_int(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    digits = re.sub(r"[^\d]", "", str(v))
    return int(digits) if digits else None
