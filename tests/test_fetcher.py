from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import respx

from news_aggregator.config import SourceConfig
from news_aggregator.fetcher import Fetcher


def _rss(items):
    body = "".join(
        f"<item><title>{t}</title><link>{link}</link><description>{d}</description>"
        f"<pubDate>{format_datetime(p)}</pubDate></item>"
        for t, link, d, p in items
    )
    return f"<?xml version='1.0'?><rss version='2.0'><channel><title>T</title>{body}</channel></rss>"


@respx.mock
async def test_fetcher_parses_filters_and_isolates_failures(config):
    now = datetime.now(timezone.utc)
    config.sources = [
        SourceConfig(name="Good", type="rss", url="https://good.test/rss", category="Business"),
        SourceConfig(name="Broken", type="rss", url="https://broken.test/rss"),
    ]
    respx.get("https://good.test/rss").mock(
        return_value=httpx.Response(
            200,
            text=_rss([
                ("Fresh story", "https://good.test/a?utm_source=rss", "&lt;b&gt;Body&lt;/b&gt;", now),
                ("Old story", "https://good.test/b", "x", now - timedelta(days=5)),
            ]),
            headers={"ETag": '"v1"'},
        )
    )
    respx.get("https://broken.test/rss").mock(return_value=httpx.Response(404))

    result = await Fetcher(config).fetch_all({})
    assert [a.title for a in result.articles] == ["Fresh story"]
    art = result.articles[0]
    assert art.url == "https://good.test/a" and art.summary == "Body"
    assert result.failed_sources == ["Broken"]
    assert result.http_cache["https://good.test/rss"]["etag"] == '"v1"'


@respx.mock
async def test_conditional_get_not_modified_is_not_a_failure(config):
    config.sources = [SourceConfig(name="Same", type="rss", url="https://same.test/rss")]
    route = respx.get("https://same.test/rss").mock(return_value=httpx.Response(304))
    result = await Fetcher(config).fetch_all({"https://same.test/rss": {"etag": '"v1"'}})
    assert route.calls[0].request.headers["If-None-Match"] == '"v1"'
    assert result.articles == [] and result.failed_sources == []
