"""Shared data models passed between pipeline stages."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import TypedDict

from pydantic import BaseModel, Field


def stable_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def int64_id(text: str) -> int:
    """Positive int64 id for FAISS, derived from a string (60 bits of sha256)."""
    return int(stable_id(text)[:15], 16)


class Article(BaseModel):
    id: str                               # sha256 of canonical URL
    source: str
    title: str
    url: str                              # canonical URL, tracking params stripped
    summary: str | None = None
    published_at: datetime
    category: str | None = None
    full_text: str | None = None
    authority_weight: float = 1.0

    @property
    def embed_text(self) -> str:
        return f"{self.title}. {self.summary or ''}".strip()


class StoryStatus(str, Enum):
    NEW = "new"
    DEVELOPING = "developing"   # seen on an earlier day, but coverage grew substantially
    REPEAT = "repeat"           # already reported, nothing new: excluded from the briefing


class TextStatus(str, Enum):
    SUMMARY_ONLY = "summary_only"
    FULL_TEXT = "full_text"


class StoryCluster(BaseModel):
    cluster_id: str
    representative: Article
    members: list[Article]
    first_seen: datetime
    last_seen: datetime
    status: StoryStatus = StoryStatus.NEW
    previous_source_count: int | None = None
    score: float = 0.0
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    text_status: TextStatus = TextStatus.SUMMARY_ONLY
    full_text: str | None = None

    @property
    def sources(self) -> list[str]:
        return sorted({m.source for m in self.members})

    @property
    def source_count(self) -> int:
        return len(self.sources)

    @property
    def member_links(self) -> list[tuple[str, str]]:
        """(source, url) pairs, one per source, representative first."""
        seen: set[str] = set()
        links: list[tuple[str, str]] = []
        for art in [self.representative, *self.members]:
            if art.source not in seen:
                seen.add(art.source)
                links.append((art.source, art.url))
        return links


class RunStats(BaseModel):
    sources_total: int = 0
    sources_failed: list[str] = Field(default_factory=list)
    items_fetched: int = 0
    items_after_exact_dedupe: int = 0
    unique_stories: int = 0
    repeats_suppressed: int = 0
    llm_duplicates_merged: int = 0
    deep_scraped: int = 0
    deep_scrape_failed: int = 0
    used_fallback_digest: bool = False


class PipelineState(TypedDict, total=False):
    run_id: str
    run_date: str
    started_at: datetime
    raw_articles: list[Article]
    clusters: list[StoryCluster]
    priority_ids: list[str]
    briefing_md: str | None
    validation_errors: list[str]
    source_errors: list[str]
    attempts: int
    stats: RunStats
    sent: bool


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
