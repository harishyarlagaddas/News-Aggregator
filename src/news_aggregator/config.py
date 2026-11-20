"""Configuration: YAML files for tuning, environment variables for deployment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CONFIG_DIR = Path(os.environ.get("NEWSAGG_CONFIG_DIR", Path.cwd() / "config"))


# ---------------------------------------------------------------------------
# Source registry (config/sources.yaml)
# ---------------------------------------------------------------------------
class SourceConfig(BaseModel):
    name: str
    type: Literal["rss", "api", "scrape"]
    url: str
    category: str | None = None
    authority_weight: float = 1.0
    enabled: bool = True
    provider: str | None = None          # api only: hackernews | guardian | newsapi
    api_key_env: str | None = None       # api only
    params: dict[str, str] = Field(default_factory=dict)
    link_selector: str | None = None     # scrape only


# ---------------------------------------------------------------------------
# Tuning (config/settings.yaml)
# ---------------------------------------------------------------------------
class FetchConfig(BaseModel):
    concurrency: int = 10
    per_host_concurrency: int = 2
    timeout_seconds: float = 10
    max_retries: int = 3
    lookback_hours: int = 36
    max_items_per_source: int = 60
    user_agent: str = "NewsInsightsAggregator/0.1"


class EditorConfig(BaseModel):
    embedding_backend: Literal["local", "bedrock", "hash"] = "local"
    local_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    bedrock_embedding_model: str = "amazon.titan-embed-text-v2:0"
    similarity_threshold: float = 0.82
    cross_day_threshold: float = 0.85
    memory_days: int = 7
    developing_growth: float = 1.5


class PriorityWeights(BaseModel):
    coverage: float = 0.40
    authority: float = 0.25
    recency: float = 0.20
    relevance: float = 0.15


class PriorityConfig(BaseModel):
    top_n: int = 8
    min_score_for_deep_scrape: float = 0.55
    recency_half_life_hours: float = 12
    weights: PriorityWeights = Field(default_factory=PriorityWeights)
    coverage_cap: int = 6
    max_chars_per_story: int = 6000
    use_browser_fallback: bool = True
    watchlist: list[str] = Field(default_factory=list)


class DedupeConfig(BaseModel):
    """LLM duplicate check on the top-ranked stories (editor/judge.py)."""

    enabled: bool = True
    model: str = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"   # Bedrock model/profile id
    thinking_budget_tokens: int = 4000   # extended thinking budget; 0 turns thinking off
    max_output_tokens: int = 1000
    max_candidates: int = 30             # how many top-ranked stories the model reviews
    max_group_size: int = 5              # bigger groups are treated as over-merging and ignored


class WriterConfig(BaseModel):
    llm_model: str = "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
    analyst_model: str | None = None
    temperature: float = 0.2
    max_theme_items: int = 20
    min_words: int = 150
    max_words: int = 2500


class EmailConfig(BaseModel):
    subject_template: str = "Daily Briefing: {date}"
    from_address: str = "briefing@example.com"
    to_addresses: list[str] = Field(default_factory=list)


class Tuning(BaseModel):
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    editor: EditorConfig = Field(default_factory=EditorConfig)
    priority: PriorityConfig = Field(default_factory=PriorityConfig)
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig)
    writer: WriterConfig = Field(default_factory=WriterConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)


# ---------------------------------------------------------------------------
# Deployment (environment variables, prefix NEWSAGG_)
# ---------------------------------------------------------------------------
class Env(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NEWSAGG_", env_file=".env", extra="ignore")

    storage_backend: Literal["local", "s3"] = "local"
    local_storage_dir: Path = Path("./data")
    s3_bucket: str | None = None
    s3_prefix: str = ""

    email_backend: Literal["ses", "smtp", "none"] = "none"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = False

    email_from: str | None = None
    email_to: str | None = None          # comma-separated override
    llm_model: str | None = None
    embedding_backend: Literal["local", "bedrock", "hash"] | None = None
    local_model_path: str | None = None  # pre-downloaded model directory (Docker image)

    aws_region: str | None = Field(default=None, validation_alias="AWS_REGION")
    log_format: Literal["json", "console"] = "json"

    @field_validator("s3_prefix")
    @classmethod
    def _strip_slashes(cls, v: str) -> str:
        return v.strip("/")


class AppConfig(BaseModel):
    sources: list[SourceConfig]
    tuning: Tuning
    env: Env

    @property
    def enabled_sources(self) -> list[SourceConfig]:
        return [s for s in self.sources if s.enabled]

    @property
    def recipients(self) -> list[str]:
        if self.env.email_to:
            return [a.strip() for a in self.env.email_to.split(",") if a.strip()]
        return self.tuning.email.to_addresses

    @property
    def sender(self) -> str:
        return self.env.email_from or self.tuning.email.from_address

    @property
    def llm_model(self) -> str:
        return self.env.llm_model or self.tuning.writer.llm_model

    @property
    def embedding_backend(self) -> str:
        return self.env.embedding_backend or self.tuning.editor.embedding_backend


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def load_config(config_dir: Path | None = None, env: Env | None = None) -> AppConfig:
    config_dir = Path(config_dir or DEFAULT_CONFIG_DIR)
    sources_raw = _read_yaml(config_dir / "sources.yaml").get("sources", [])
    tuning_raw = _read_yaml(config_dir / "settings.yaml")
    return AppConfig(
        sources=[SourceConfig(**s) for s in sources_raw],
        tuning=Tuning(**tuning_raw),
        env=env or Env(),
    )
