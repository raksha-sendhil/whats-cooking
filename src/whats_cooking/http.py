"""Shared HTTP client: retries, Retry-After, per-host pacing, optional auth."""

from __future__ import annotations

import logging
import os
import threading
import time
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; whats_cooking/0.1; personal research feed)"

# Minimum seconds between requests to the same host (politeness / documented limits).
DEFAULT_PACING = {
    "export.arxiv.org": 3.0,
    "rss.arxiv.org": 1.0,
    "www.reddit.com": 7.0,   # unauthenticated RSS 429s at faster rates
    "api.github.com": 0.5,
    "github.com": 1.0,
    "api.stackexchange.com": 0.5,
    "public.api.bsky.app": 0.2,
}

RETRY_STATUS = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status


class Fetcher:
    def __init__(self, timeout: float = 30, retries: int = 3, pacing: dict[str, float] | None = None):
        self.client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
        self.retries = retries
        self.pacing = {**DEFAULT_PACING, **(pacing or {})}
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._locks_guard = threading.Lock()
        self.github_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

    def _pace(self, host: str) -> None:
        gap = self.pacing.get(host, 0.0)
        if gap <= 0:
            return
        with self._locks_guard:
            lock = self._host_locks.setdefault(host, threading.Lock())
        with lock:
            wait = self._host_last.get(host, 0.0) + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._host_last[host] = time.monotonic()

    def get(self, url: str, params: dict | None = None, headers: dict | None = None,
            retries: int | None = None) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        hdrs = dict(headers or {})
        if host == "api.github.com" and self.github_token:
            hdrs.setdefault("Authorization", f"Bearer {self.github_token}")
        retries = self.retries if retries is None else retries
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            self._pace(host)
            try:
                resp = self.client.get(url, params=params, headers=hdrs)
            except httpx.TransportError as exc:
                last_exc = exc
                log.debug("transport error %s (attempt %d): %s", url, attempt, exc)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in RETRY_STATUS and attempt < retries:
                delay = _retry_after(resp) or 2 ** (attempt + 1)
                if delay > 90:
                    break  # not worth blocking the run; surface the error
                log.debug("HTTP %s from %s, retrying in %.0fs", resp.status_code, host, delay)
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise HttpError(url, resp.status_code, resp.text)
            return resp
        if last_exc:
            raise last_exc
        raise HttpError(url, resp.status_code, resp.text)

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        return self.get(url, params=params, headers=headers).json()

    def get_text(self, url: str, params: dict | None = None, headers: dict | None = None) -> str:
        return self.get(url, params=params, headers=headers).text

    def post_json(self, url: str, payload: dict, headers: dict | None = None):
        host = urlsplit(url).hostname or ""
        self._pace(host)
        resp = self.client.post(url, json=payload, headers=headers or {})
        if resp.status_code >= 400:
            raise HttpError(url, resp.status_code, resp.text)
        return resp.json()

    def close(self) -> None:
        self.client.close()


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value and value.isdigit():
        return float(value)
    reset = resp.headers.get("x-ratelimit-reset")
    if resp.headers.get("x-ratelimit-remaining") == "0" and reset and reset.isdigit():
        return max(1.0, float(reset) - time.time())
    return None
