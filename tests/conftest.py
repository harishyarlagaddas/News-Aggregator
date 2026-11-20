from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from news_aggregator.config import Env, load_config
from news_aggregator.fetcher.canonical import canonicalize_url
from news_aggregator.models import Article, stable_id, utcnow

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def make_article(
    title: str,
    source: str = "Outlet A",
    url: str | None = None,
    summary: str = "",
    hours_ago: float = 1.0,
    weight: float = 1.0,
    category: str = "Business",
) -> Article:
    url = canonicalize_url(url or f"https://{source.replace(' ', '').lower()}.com/{stable_id(title)[:10]}")
    return Article(
        id=stable_id(url),
        source=source,
        title=title,
        url=url,
        summary=summary,
        published_at=utcnow() - timedelta(hours=hours_ago),
        category=category,
        authority_weight=weight,
    )


@pytest.fixture
def config(tmp_path):
    env = Env(
        storage_backend="local",
        local_storage_dir=tmp_path / "data",
        email_backend="none",
        embedding_backend="hash",
        log_format="console",
    )
    cfg = load_config(CONFIG_DIR, env=env)
    # Hash embeddings are lexical, so use thresholds that suit them.
    cfg.tuning.editor.similarity_threshold = 0.6
    cfg.tuning.editor.cross_day_threshold = 0.6
    cfg.tuning.priority.use_browser_fallback = False
    return cfg
