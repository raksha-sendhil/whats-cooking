from datetime import UTC, datetime

from conftest import FakeFetcher

from whats_cooking.collectors import Context
from whats_cooking.collectors.discourse import Discourse
from whats_cooking.collectors.hackernews import HackerNews
from whats_cooking.collectors.pages import Pages, clean_title
from whats_cooking.collectors.reddit import Reddit

NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


def _ctx(routes, known=frozenset()):
    return Context(http=FakeFetcher(routes), known_channels=known, now=NOW)


def test_hackernews_kinds_and_front_page():
    hit = lambda oid, title, tags, url=None: {
        "objectID": oid, "title": title, "_tags": tags, "url": url, "points": 50,
        "num_comments": 9, "created_at_i": int(NOW.timestamp()) - 3600, "author": "pg",
    }
    routes = {  # first matching substring wins
        "search_by_date": {"hits": [
            hit("1", "A story", ["story"], "https://x.example/a"),
            hit("2", "Show HN: Thing", ["story", "show_hn"], "https://x.example/b"),
            hit("3", "Ask HN: Why?", ["story", "ask_hn"]),
        ], "nbPages": 1},
        "/v1/search": {"hits": [{"objectID": "1"}]},  # front page
    }
    items = {i.native_id: i for i in HackerNews({}, _ctx(routes)).collect()}
    assert items["1"].kind == "story" and items["1"].metrics["front_page"] == 1
    assert items["2"].kind == "show"
    assert items["3"].kind == "ask" and items["3"].url == "https://news.ycombinator.com/item?id=3"


def test_discourse_filters_spam_and_low_activity():
    topics = [
        {"id": 1, "slug": "s", "title": "Feature request: offline mode", "like_count": 5, "reply_count": 3},
        {"id": 2, "slug": "s", "title": "7742531843 mumbai call girl service", "like_count": 9, "reply_count": 9},
        {"id": 3, "slug": "s", "title": "quiet topic", "like_count": 0, "reply_count": 0},
        {"id": 4, "slug": "s", "title": "Pinned rules", "like_count": 50, "reply_count": 50, "pinned": True},
    ]
    c = Discourse({"forums": ["forum.example"]}, _ctx({"forum.example": {"topic_list": {"topics": topics}}}))
    assert [i.native_id for i in c.collect()] == ["forum.example:1"]


def test_reddit_stops_after_sunset():
    c = Reddit({"sunset": "2026-10-01", "subreddits": ["x"]}, _ctx({}))
    assert c.skip_reason() == "past sunset date 2026-10-01"
    assert Reddit({"sunset": "2026-11-13"}, _ctx({})).skip_reason() is None


def test_reddit_waits_once_on_429_then_moves_on():
    from whats_cooking.http import HttpError

    feed = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>t3_a</id><title>Post A</title><link href="https://www.reddit.com/r/b/comments/a/"/>
      <updated>2026-10-07T10:00:00+00:00</updated></entry></feed>"""

    class Flaky:
        def __init__(self):
            self.calls = 0

        def get(self, url, params=None, headers=None, retries=None):
            self.calls += 1
            if "/r/a/" in url:
                raise HttpError(url, 429)
            class R:
                content = feed.encode()
            return R()

    http = Flaky()
    c = Reddit({"subreddits": ["a", "b"], "cooldown_seconds": 0}, Context(http=http, now=NOW))
    items = list(c.collect())
    assert [i.channel for i in items] == ["r/b"]
    assert http.calls == 3  # a, a (after cooldown), b
    assert len(c.errors) == 1 and c.errors[0].startswith("a:")


def test_one_failing_feed_does_not_stop_the_rest():
    routes = {"good.example": {"topic_list": {"topics": [
        {"id": 1, "slug": "s", "title": "Works fine here", "like_count": 9, "reply_count": 9}]}},
        "bad.example": RuntimeError("boom")}
    c = Discourse({"forums": ["bad.example", "good.example"]}, _ctx(routes))
    assert len(list(c.collect())) == 1 and len(c.errors) == 1


def test_pages_titles_and_baseline():
    html = """
      <a href="/news/cyber-program"><span>Announcements</span><span>Oct 6, 2026</span>
         <h4>Expanding the Cyber Verification Program</h4><p>We're launching...</p></a>
      <a href="/news/cyber-program">FEATURED</a>
      <a href="/news">News</a>
      <a href="/careers/x">Careers</a>
    """
    cfg = {"pages": [{"name": "lab", "url": "https://lab.example/news", "pattern": r"lab\.example/news/[^/]+$"}]}
    items = list(Pages(cfg, _ctx({"lab.example": html})).collect())
    assert [i.title for i in items] == ["Expanding the Cyber Verification Program"]
    assert items[0].extra == {"baseline": True}
    later = list(Pages(cfg, _ctx({"lab.example": html}, known=frozenset({("pages", "lab")}))).collect())
    assert later[0].extra == {}


def test_clean_title_strips_dates_categories_bylines():
    assert clean_title("Oct 2, 2026 Announcements Anthropic invests $100 million") == "Anthropic invests $100 million"
    assert clean_title("Research Introducing Mistral Large 4 October 6, 2026 By Mistral") == "Introducing Mistral Large 4"
