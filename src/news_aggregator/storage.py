"""Blob storage for the FAISS index, metadata, HTTP cache and archived briefings.

Local disk for development, S3 on AWS. Keys are relative paths like
``index/index.faiss`` or ``briefings/2026-11-16.md``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .config import AppConfig


class Storage(Protocol):
    def read_bytes(self, key: str) -> bytes | None: ...
    def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None: ...
    def describe(self, key: str) -> str: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key

    def read_bytes(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.exists() else None

    def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic on the same filesystem

    def describe(self, key: str) -> str:
        return str(self._path(key))


class S3Storage:
    def __init__(self, bucket: str, prefix: str = "", client=None):
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = client or boto3.client("s3")

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def read_bytes(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                return None
            raise
        return obj["Body"].read()

    def write_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, **extra)

    def describe(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"


def build_storage(config: AppConfig) -> Storage:
    env = config.env
    if env.storage_backend == "s3":
        if not env.s3_bucket:
            raise ValueError("NEWSAGG_S3_BUCKET is required when NEWSAGG_STORAGE_BACKEND=s3")
        return S3Storage(env.s3_bucket, env.s3_prefix)
    return LocalStorage(env.local_storage_dir)
