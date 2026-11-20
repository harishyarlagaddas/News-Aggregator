"""Priority scoring: decides which stories lead the briefing and earn a full-text scrape.

score = w_cov * coverage + w_auth * authority + w_rec * recency + w_rel * relevance
All components are in [0, 1].
"""

from __future__ import annotations

from datetime import datetime

import numpy as np

from ..config import PriorityConfig
from ..models import StoryCluster, StoryStatus, utcnow


class PriorityScorer:
    def __init__(self, cfg: PriorityConfig, watchlist_vectors: np.ndarray | None = None):
        self.cfg = cfg
        self.watchlist = watchlist_vectors

    def score(
        self,
        clusters: list[StoryCluster],
        centroids: dict[str, np.ndarray],
        now: datetime | None = None,
    ) -> list[StoryCluster]:
        """Score clusters in place and return non-repeat clusters, best first."""
        now = now or utcnow()
        w = self.cfg.weights
        candidates = [c for c in clusters if c.status != StoryStatus.REPEAT]
        if not candidates:
            return []
        max_authority = max(m.authority_weight for c in candidates for m in c.members) or 1.0

        for c in candidates:
            coverage = min(c.source_count / self.cfg.coverage_cap, 1.0)
            authority = max(m.authority_weight for m in c.members) / max_authority
            newest = max(m.published_at for m in c.members)
            age_h = max((now - newest).total_seconds() / 3600, 0.0)
            recency = 0.5 ** (age_h / self.cfg.recency_half_life_hours)
            relevance = 0.0
            if self.watchlist is not None and len(self.watchlist):
                sims = self.watchlist @ centroids[c.cluster_id]
                # Map cosine (~0.1 unrelated .. ~0.6 on-topic for MiniLM) onto [0, 1].
                relevance = float(np.clip((sims.max() - 0.15) / 0.45, 0.0, 1.0))

            parts = {
                "coverage": round(coverage, 3),
                "authority": round(authority, 3),
                "recency": round(recency, 3),
                "relevance": round(relevance, 3),
            }
            c.score_breakdown = parts
            c.score = round(
                w.coverage * coverage
                + w.authority * authority
                + w.recency * recency
                + w.relevance * relevance,
                4,
            )

        return sorted(candidates, key=lambda c: (-c.score, -c.source_count))

    def select(self, ranked: list[StoryCluster]) -> tuple[list[str], list[str]]:
        """Returns (top story ids, ids that should be deep-scraped)."""
        top = ranked[: self.cfg.top_n]
        deep = [c.cluster_id for c in top if c.score >= self.cfg.min_score_for_deep_scrape]
        return [c.cluster_id for c in top], deep
