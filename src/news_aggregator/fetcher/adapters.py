"""Source adapters: RSS/Atom, official APIs, and HTML index-page scraping.

Each adapter returns a list of ``Article``. Errors propagate to the runner, which
isolates them per source so one broken feed never fails the run.
"""

from __future__ import annotations

import asyncio
import json
import os
from calendar import timegm
from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urljoin

import feedparser
from selectolax.lexbor import LexborHTMLParser

from ..config import SourceConfig
from ..models import Article, stable_id, utcnow
from .canonical import canonicalize_url, clean_text
from .http import PoliteClient


class Adapter(Protocol):
    async def fetch(self, source: SourceConfig, http: PoliteClient, limit: int) -> list[Article]: ...


def make_article(
    source: SourceConfig,
    *,
    title: str,
    url: str,
    summary: str | None,
    published_at: datetime | None,
) -> Article | None:
    title = clean_text(title)
    if not title or not url:
        return None
    canonical = canonicalize_url(url)
    summary_clean = clean_text(summary)[:1500] or None
    return Article(
        id=stable_id(canonical),
        source=source.name,
        title=title,
        url=canonical,
        summary=summary_clean,
        published_at=published_at or utcnow(),
        category=source.category,
        authority_weight=source.authority_weight,
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# RSS / Atom
# ---------------------------------------------------------------------------
class RSSAdapter:
    async def fetch(self, source: SourceConfig, http: PoliteClient, limit: int) -> list[Article]:
        # Feeds are meant to be polled, so robots.txt is not consulted for them.
        resp = await http.get(source.url, conditional=True, check_robots=False)
        # feedparser is CPU-bound and synchronous; keep it off the event loop.
        feed = await asyncio.to_thread(feedparser.parse, resp.text)
        if feed.bozo and not feed.entries:
            raise ValueError(f"unparseable feed: {feed.bozo_exception!r}")

        articles: list[Article] = []
        for entry in feed.entries[:limit]:
            parsed = entry.get("published_parsed") or entry.get("updated_parsed")
            published = (
                datetime.fromtimestamp(timegm(parsed), tz=timezone.utc) if parsed else None
            )
            summary = entry.get("summary") or entry.get("description")
            art = make_article(
                source,
                title=entry.get("title", ""),
                url=entry.get("link", ""),
                summary=summary,
                published_at=published,
            )
            if art:
                articles.append(art)
        return articles


# ---------------------------------------------------------------------------
# Official APIs
# ---------------------------------------------------------------------------
class APIAdapter:
    async def fetch(self, source: SourceConfig, http: PoliteClient, limit: int) -> list[Article]:
        provider = (source.provider or "").lower()
        handler = {
            "hackernews": self._hackernews,
            "guardian": self._guardian,
            "newsapi": self._newsapi,
        }.get(provider)
        if handler is None:
            raise ValueError(f"unknown API provider {source.provider!r}")
        return await handler(source, http, limit)

    @staticmethod
    def _key(source: SourceConfig) -> str:
        key = os.environ.get(source.api_key_env or "", "")
        if not key:
            raise ValueError(f"missing API key env var {source.api_key_env}")
        return key

    async def _hackernews(self, source, http, limit) -> list[Article]:
        params = {"hitsPerPage": str(limit), **source.params}
        resp = await http.get(source.url, params=params, check_robots=False)
        hits = json.loads(resp.text).get("hits", [])
        out = []
        for h in hits:
            url = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            art = make_article(
                source,
                title=h.get("title") or "",
                url=url,
                summary=h.get("story_text"),
                published_at=_parse_iso(h.get("created_at")),
            )
            if art:
                out.append(art)
        return out

    async def _guardian(self, source, http, limit) -> list[Article]:
        params = {
            "api-key": self._key(source),
            "page-size": str(min(limit, 50)),
            "order-by": "newest",
            "show-fields": "trailText",
            **source.params,
        }
        resp = await http.get(source.url, params=params, check_robots=False)
        results = json.loads(resp.text).get("response", {}).get("results", [])
        out = []
        for r in results:
            art = make_article(
                source,
                title=r.get("webTitle", ""),
                url=r.get("webUrl", ""),
                summary=(r.get("fields") or {}).get("trailText"),
                published_at=_parse_iso(r.get("webPublicationDate")),
            )
            if art:
                out.append(art)
        return out

    async def _newsapi(self, source, http, limit) -> list[Article]:
        params = {"pageSize": str(min(limit, 100)), **source.params}
        resp = await http.get(
            source.url, params=params, check_robots=False, headers={"X-Api-Key": self._key(source)}
        )
        out = []
        for a in json.loads(resp.text).get("articles", []):
            art = make_article(
                source,
                title=a.get("title") or "",
                url=a.get("url") or "",
                summary=a.get("description"),
                published_at=_parse_iso(a.get("publishedAt")),
            )
            if art:
                out.append(art)
        return out


# ---------------------------------------------------------------------------
# HTML index page (last resort)
# ---------------------------------------------------------------------------
class ScrapeAdapter:
    async def fetch(self, source: SourceConfig, http: PoliteClient, limit: int) -> list[Article]:
        if not source.link_selector:
            raise ValueError("scrape sources need link_selector")
        resp = await http.get(source.url)  # respects robots.txt
        tree = HTMLParser(resp.text)
        out: list[Article] = []
        for node in tree.css(source.link_selector)[:limit]:
            href = node.attributes.get("href")
            if not href:
                continue
            art = make_article(
                source,
                title=node.text(strip=True),
                url=urljoin(resp.url, href),
                summary=None,
                published_at=None,  # unknown; treated as "now"
            )
            if art:
                out.append(art)
        return out


ADAPTERS: dict[str, Adapter] = {
    "rss": RSSAdapter(),
    "api": APIAdapter(),
    "scrape": ScrapeAdapter(),
}
