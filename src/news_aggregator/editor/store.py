"""Cross-day story memory: a FAISS index plus a Parquet metadata sidecar, stored as files.

On AWS both files live in S3. Each run:
  1. ``load``    downloads index/index.faiss and index/metadata.parquet (empty on first run)
  2. ``search``  finds yesterday's stories that match today's clusters
  3. ``upsert`` / ``prune`` update the in-memory copy
  4. ``save``    uploads both files, only after the briefing was sent successfully

The index is exact (IndexFlatIP over normalised vectors = cosine similarity). At a
7-day window of a few thousand stories that is milliseconds per query.
"""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass
from datetime import datetime

if sys.platform == "darwin":
    # Load torch's OpenMP runtime before FAISS's. The reverse order (faiss first, torch
    # lazily inside LocalEmbedder) segfaults on macOS. See news_aggregator/__init__.py.
    try:
        import torch  # noqa: F401
    except ImportError:  # bedrock / hash backends do not need torch
        pass

import faiss
import numpy as np
import pandas as pd

from ..logging_setup import log
from ..storage import Storage

if sys.platform == "darwin":
    faiss.omp_set_num_threads(1)

INDEX_KEY = "index/index.faiss"
META_KEY = "index/metadata.parquet"

META_COLUMNS = {
    "faiss_id": "int64",
    "cluster_id": "string",
    "title": "string",
    "url": "string",
    "reported_source_count": "int64",
    "first_seen": "datetime64[ns, UTC]",
    "last_seen": "datetime64[ns, UTC]",
    "run_id": "string",
}


@dataclass
class MemoryEntry:
    faiss_id: int
    cluster_id: str
    title: str
    url: str
    reported_source_count: int
    first_seen: datetime
    last_seen: datetime


@dataclass
class Match:
    similarity: float
    entry: MemoryEntry


def _empty_meta() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=t) for c, t in META_COLUMNS.items()})


class StoryMemory:
    def __init__(self, dim: int, index: faiss.Index | None = None, meta: pd.DataFrame | None = None):
        self.dim = dim
        self.index = index or faiss.IndexIDMap(faiss.IndexFlatIP(dim))
        self.meta = meta if meta is not None else _empty_meta()

    # -- persistence ----------------------------------------------------------
    @classmethod
    def load(cls, storage: Storage, dim: int) -> "StoryMemory":
        index_bytes = storage.read_bytes(INDEX_KEY)
        meta_bytes = storage.read_bytes(META_KEY)
        if index_bytes is None or meta_bytes is None:
            log.info("memory_empty", reason="no index files yet")
            return cls(dim)

        index = faiss.deserialize_index(np.frombuffer(index_bytes, dtype=np.uint8))
        if index.d != dim:
            # Embedding model changed: old vectors are not comparable. Start fresh.
            log.warning("memory_reset", reason="dimension changed", old=index.d, new=dim)
            return cls(dim)
        meta = pd.read_parquet(io.BytesIO(meta_bytes))
        mem = cls(dim, index, meta)
        mem._reconcile()
        log.info("memory_loaded", stories=mem.size)
        return mem

    def save(self, storage: Storage, run_id: str) -> None:
        self.meta["run_id"] = run_id
        buf = io.BytesIO()
        self.meta.to_parquet(buf, index=False)
        # Metadata first: if the index upload fails, _reconcile() repairs the pair on next load.
        storage.write_bytes(META_KEY, buf.getvalue(), "application/octet-stream")
        storage.write_bytes(
            INDEX_KEY, faiss.serialize_index(self.index).tobytes(), "application/octet-stream"
        )
        log.info("memory_saved", stories=self.size, location=storage.describe(INDEX_KEY))

    def _index_ids(self) -> np.ndarray:
        return faiss.vector_to_array(self.index.id_map).astype(np.int64)

    def _reconcile(self) -> None:
        """Keep only ids present in both the index and the metadata."""
        index_ids = set(self._index_ids().tolist())
        meta_ids = set(self.meta["faiss_id"].astype("int64").tolist())
        if index_ids == meta_ids:
            return
        log.warning(
            "memory_out_of_sync",
            index_only=len(index_ids - meta_ids),
            meta_only=len(meta_ids - index_ids),
        )
        extra = np.array(sorted(index_ids - meta_ids), dtype=np.int64)
        if len(extra):
            self.index.remove_ids(extra)
        self.meta = self.meta[self.meta["faiss_id"].isin(index_ids)].reset_index(drop=True)

    # -- queries --------------------------------------------------------------
    @property
    def size(self) -> int:
        return int(self.index.ntotal)

    def search(self, vectors: np.ndarray) -> list[Match | None]:
        """Nearest stored story for each query vector (or None if memory is empty)."""
        if self.size == 0 or len(vectors) == 0:
            return [None] * len(vectors)
        sims, ids = self.index.search(np.ascontiguousarray(vectors, dtype=np.float32), 1)
        by_id = self.meta.set_index("faiss_id")
        out: list[Match | None] = []
        for sim, fid in zip(sims[:, 0], ids[:, 0]):
            if fid < 0 or fid not in by_id.index:
                out.append(None)
                continue
            row = by_id.loc[fid]
            out.append(
                Match(
                    similarity=float(sim),
                    entry=MemoryEntry(
                        faiss_id=int(fid),
                        cluster_id=str(row["cluster_id"]),
                        title=str(row["title"]),
                        url=str(row["url"]),
                        reported_source_count=int(row["reported_source_count"]),
                        first_seen=row["first_seen"].to_pydatetime(),
                        last_seen=row["last_seen"].to_pydatetime(),
                    ),
                )
            )
        return out

    # -- mutations ------------------------------------------------------------
    def upsert(self, entries: list[MemoryEntry], vectors: np.ndarray) -> None:
        if not entries:
            return
        ids = np.array([e.faiss_id for e in entries], dtype=np.int64)
        self.index.remove_ids(ids)  # no-op for ids that are not present
        self.index.add_with_ids(np.ascontiguousarray(vectors, dtype=np.float32), ids)

        new_rows = pd.DataFrame(
            {
                "faiss_id": ids,
                "cluster_id": [e.cluster_id for e in entries],
                "title": [e.title for e in entries],
                "url": [e.url for e in entries],
                "reported_source_count": [e.reported_source_count for e in entries],
                "first_seen": pd.to_datetime([e.first_seen for e in entries], utc=True),
                "last_seen": pd.to_datetime([e.last_seen for e in entries], utc=True),
                "run_id": "",
            }
        ).astype(META_COLUMNS)
        kept = self.meta[~self.meta["faiss_id"].isin(ids)]
        self.meta = pd.concat([kept, new_rows], ignore_index=True) if len(kept) else new_rows

    def prune(self, older_than: datetime) -> int:
        """Drop stories not seen since ``older_than``. Returns the number removed."""
        if self.size == 0:
            return 0
        stale = self.meta["last_seen"] < pd.Timestamp(older_than)
        stale_ids = self.meta.loc[stale, "faiss_id"].to_numpy(dtype=np.int64)
        if len(stale_ids):
            self.index.remove_ids(stale_ids)
            self.meta = self.meta[~stale].reset_index(drop=True)
        return int(len(stale_ids))
