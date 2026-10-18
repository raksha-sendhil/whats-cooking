from __future__ import annotations

from pathlib import Path

import pytest

from whats_cooking.config import Settings
from whats_cooking.db import DB


@pytest.fixture
def db():
    d = DB(":memory:")
    yield d
    d.close()


@pytest.fixture
def settings(tmp_path: Path):
    return Settings(root=tmp_path, db_path=tmp_path / "t.db", sources={}, grouping={})


class FakeFetcher:
    """Serves canned responses by URL substring."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[str] = []

    def _match(self, url):
        self.calls.append(url)
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected URL {url}")

    def get_json(self, url, params=None, headers=None):
        return self._match(url)

    def get_text(self, url, params=None, headers=None):
        return self._match(url)

    def get(self, url, params=None, headers=None, retries=None):
        body = self._match(url)

        class R:
            content = body.encode() if isinstance(body, str) else body
            text = body if isinstance(body, str) else body.decode()
        return R()
