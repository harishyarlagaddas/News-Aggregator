"""The Editor: collapse overlapping coverage into story clusters before anything hits the LLM.

Pass 1  exact:     identical canonical URLs are dropped; identical normalised titles are linked
Pass 2  semantic:  cosine similarity >= threshold links articles; connected components = stories
Pass 3  cross-day: each story is matched against the FAISS memory of previous days
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..config import EditorConfig
from ..fetcher.canonical import normalize_title
from ..models import Article, StoryCluster, StoryStatus, int64_id, utcnow
from .embed import Embedder, l2_normalize
from .store import MemoryEntry, StoryMemory


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def exact_dedupe(articles: list[Article]) -> list[Article]:
    """Drop exact URL duplicates, keeping the copy from the highest-authority source."""
    best: dict[str, Article] = {}
    for art in articles:
        cur = best.get(art.id)
        if cur is None or (art.authority_weight, -art.published_at.timestamp()) > (
            cur.authority_weight,
            -cur.published_at.timestamp(),
        ):
            best[art.id] = art
    return list(best.values())


def similarity_pairs(vectors: np.ndarray, threshold: float, chunk: int = 1024):
    """Yield (i, j) with i < j and cosine(vectors[i], vectors[j]) >= threshold."""
    n = len(vectors)
    for start in range(0, n, chunk):
        block = vectors[start : start + chunk] @ vectors.T
        rows, cols = np.nonzero(block >= threshold)
        for r, c in zip(rows, cols):
            i = start + int(r)
            if i < c:
                yield i, int(c)


def _representative(members: list[Article]) -> Article:
    return max(members, key=lambda a: (a.authority_weight, -a.published_at.timestamp()))


@dataclass
class EditorResult:
    clusters: list[StoryCluster]
    centroids: dict[str, np.ndarray]      # cluster_id -> normalised centroid
    memory_ids: dict[str, int]            # cluster_id -> faiss id
    items_after_exact: int
    repeats: int


class Editor:
    def __init__(self, cfg: EditorConfig, embedder: Embedder):
        self.cfg = cfg
        self.embedder = embedder

    def cluster(self, articles: list[Article]) -> tuple[list[list[Article]], np.ndarray]:
        """Group same-day articles into stories. Returns member lists and their centroids."""
        if not articles:
            return [], np.zeros((0, self.embedder.dim), dtype=np.float32)

        vecs = self.embedder.embed([a.embed_text for a in articles])
        uf = UnionFind(len(articles))

        # Pass 1b: identical titles (syndicated copies on different URLs).
        by_title: dict[str, int] = {}
        for i, art in enumerate(articles):
            key = normalize_title(art.title)
            if key in by_title:
                uf.union(by_title[key], i)
            else:
                by_title[key] = i

        # Pass 2: semantic similarity. Connected components can chain loosely related items
        # (A~B, B~C); a high threshold keeps this rare, and borderline pairs are logged by tests.
        for i, j in similarity_pairs(vecs, self.cfg.similarity_threshold):
            uf.union(i, j)

        groups: dict[int, list[int]] = {}
        for i in range(len(articles)):
            groups.setdefault(uf.find(i), []).append(i)

        member_lists = [[articles[i] for i in idxs] for idxs in groups.values()]
        centroids = l2_normalize(np.stack([vecs[idxs].mean(axis=0) for idxs in groups.values()]))
        return member_lists, centroids

    def run(self, articles: list[Article], memory: StoryMemory) -> EditorResult:
        unique = exact_dedupe(articles)
        member_lists, centroids = self.cluster(unique)
        matches = memory.search(centroids)

        by_id: dict[str, StoryCluster] = {}
        centroid_map: dict[str, np.ndarray] = {}
        memory_ids: dict[str, int] = {}
        now = utcnow()

        for members, centroid, match in zip(member_lists, centroids, matches):
            # Two of today's clusters can match the same earlier story: merge them.
            if (
                match is not None
                and match.similarity >= self.cfg.cross_day_threshold
                and match.entry.cluster_id in by_id
            ):
                existing = by_id[match.entry.cluster_id]
                merged = existing.members + members
                existing.members = sorted(merged, key=lambda a: -a.authority_weight)
                existing.representative = _representative(merged)
                self._classify(existing, existing.previous_source_count or 0)
                centroid_map[existing.cluster_id] = l2_normalize(
                    centroid_map[existing.cluster_id] + centroid
                )[0]
                continue

            rep = _representative(members)
            first_seen = min(m.published_at for m in members)
            cluster = StoryCluster(
                cluster_id=rep.id[:16],
                representative=rep,
                members=sorted(members, key=lambda a: -a.authority_weight),
                first_seen=first_seen,
                last_seen=now,
            )

            if match is not None and match.similarity >= self.cfg.cross_day_threshold:
                prev = match.entry
                cluster.cluster_id = prev.cluster_id
                cluster.first_seen = prev.first_seen
                cluster.previous_source_count = prev.reported_source_count
                self._classify(cluster, prev.reported_source_count)
                memory_ids[cluster.cluster_id] = prev.faiss_id
            else:
                memory_ids[cluster.cluster_id] = int64_id(cluster.cluster_id)

            centroid_map[cluster.cluster_id] = centroid
            by_id[cluster.cluster_id] = cluster

        clusters = list(by_id.values())
        repeats = sum(c.status == StoryStatus.REPEAT for c in clusters)
        return EditorResult(clusters, centroid_map, memory_ids, len(unique), repeats)

    def _classify(self, cluster: StoryCluster, previous_count: int) -> None:
        """Earlier story resurfaces as DEVELOPING only if coverage grew substantially."""
        grew = (
            cluster.source_count >= previous_count * self.cfg.developing_growth
            and cluster.source_count >= previous_count + 2
        )
        cluster.status = StoryStatus.DEVELOPING if grew else StoryStatus.REPEAT


def remember(
    memory: StoryMemory,
    clusters: list[StoryCluster],
    result: EditorResult,
) -> None:
    """Record the clusters that appeared in today's briefing (call only after a successful send).

    Repeats refresh ``last_seen`` (so ongoing stories stay suppressed) but keep the
    source-count baseline from when they were last reported.
    """
    entries: list[MemoryEntry] = []
    vecs: list[np.ndarray] = []
    for c in clusters:
        baseline = c.source_count
        if c.status == StoryStatus.REPEAT and c.previous_source_count is not None:
            baseline = c.previous_source_count
        entries.append(
            MemoryEntry(
                faiss_id=result.memory_ids[c.cluster_id],
                cluster_id=c.cluster_id,
                title=c.representative.title,
                url=c.representative.url,
                reported_source_count=baseline,
                first_seen=c.first_seen,
                last_seen=c.last_seen,
            )
        )
        vecs.append(result.centroids[c.cluster_id])
    if entries:
        memory.upsert(entries, np.stack(vecs))
