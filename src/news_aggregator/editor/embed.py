"""Embedding backends. All return L2-normalised float32 arrays, so dot product = cosine."""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

import numpy as np

from ..config import AppConfig


def l2_normalize(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        x = x[None, :]
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (x / norms).astype(np.float32)


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


class LocalEmbedder:
    """sentence-transformers on CPU. Free, and no article text leaves the machine."""

    def __init__(self, model_name_or_path: str):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_name_or_path, device="cpu")
        self.dim = int(self.model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        vecs = self.model.encode(
            texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False
        )
        return l2_normalize(vecs)


class BedrockEmbedder:
    """Amazon Titan Text Embeddings v2 via Bedrock (one request per text)."""

    def __init__(self, model_id: str, dim: int = 512, region: str | None = None, workers: int = 8):
        import boto3

        self.client = boto3.client("bedrock-runtime", region_name=region)
        self.model_id = model_id
        self.dim = dim
        self.workers = workers

    def _one(self, text: str) -> list[float]:
        body = json.dumps({"inputText": text[:8000], "dimensions": self.dim, "normalize": True})
        resp = self.client.invoke_model(modelId=self.model_id, body=body)
        return json.loads(resp["body"].read())["embedding"]

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        with ThreadPoolExecutor(self.workers) as pool:
            vecs = list(pool.map(self._one, texts))
        return l2_normalize(np.array(vecs))


class HashEmbedder:
    """Deterministic bag-of-words hashing embedder.

    Not semantically meaningful; used for tests and offline dry runs only.
    """

    _token = re.compile(r"[a-z0-9]+")

    def __init__(self, dim: int = 512):
        self.dim = dim

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in self._token.findall(text.lower()):
                if len(tok) < 3:
                    continue
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                out[i, h % self.dim] += 1.0 if (h >> 64) % 2 else -1.0
        return l2_normalize(out)


def build_embedder(config: AppConfig) -> Embedder:
    backend = config.embedding_backend
    ed = config.tuning.editor
    if backend == "bedrock":
        return BedrockEmbedder(ed.bedrock_embedding_model, region=config.env.aws_region)
    if backend == "hash":
        return HashEmbedder()
    return LocalEmbedder(config.env.local_model_path or ed.local_model)
