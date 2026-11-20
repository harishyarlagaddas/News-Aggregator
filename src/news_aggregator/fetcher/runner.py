"""The Fetcher: pulls every enabled source concurrently and isolates failures."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

from ..config import AppConfig, SourceConfig
from ..logging_setup import log
from ..models import Article, utcnow
from .adapters import ADAPTERS
from .http import NotModified, PoliteClient


@dataclass
class FetchResult:
    articles: list[Article]
    failed_sources: list[str] = field(default_factory=list)
    http_cache: dict[str, dict[str, str]] = field(default_factory=dict)


class Fetcher:
    def __init__(self, config: AppConfig):
        self.config = config

    async def _fetch_one(self, source: SourceConfig, http: PoliteClient) -> list[Article]:
        adapter = ADAPTERS[source.type]
        return await adapter.fetch(source, http, self.config.tuning.fetch.max_items_per_source)

    async def fetch_all(self, http_cache: dict[str, dict[str, str]] | None = None) -> FetchResult:
        cfg = self.config.tuning.fetch
        sources = self.config.enabled_sources
        cache = dict(http_cache or {})
        cutoff = utcnow() - timedelta(hours=cfg.lookback_hours)

        async with PoliteClient(cfg, cache) as http:
            results = await asyncio.gather(
                *(self._fetch_one(s, http) for s in sources), return_exceptions=True
            )

        articles: list[Article] = []
        failed: list[str] = []
        for source, result in zip(sources, results):
            if isinstance(result, NotModified):
                log.info("source_not_modified", source=source.name)
                continue
            if isinstance(result, BaseException):
                failed.append(source.name)
                log.warning("source_failed", source=source.name, error=repr(result)[:300])
                continue
            fresh = [a for a in result if a.published_at >= cutoff]
            log.info("source_fetched", source=source.name, items=len(result), fresh=len(fresh))
            articles.extend(fresh)

        return FetchResult(articles=articles, failed_sources=failed, http_cache=cache)
