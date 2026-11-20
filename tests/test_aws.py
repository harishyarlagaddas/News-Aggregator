"""S3 storage and SES sending against moto's in-memory AWS."""

import boto3
import pytest
from moto import mock_aws

from news_aggregator.delivery import SESSender, render_html
from news_aggregator.editor import Editor, StoryMemory, remember
from news_aggregator.editor.embed import HashEmbedder
from news_aggregator.storage import S3Storage

from .conftest import make_article


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        yield


def test_s3_storage_roundtrip_and_missing_key(aws):
    boto3.client("s3").create_bucket(Bucket="b")
    s = S3Storage("b", "prod/")
    assert s.read_bytes("index/index.faiss") is None
    s.write_bytes("x/y.txt", b"hello", "text/plain")
    assert s.read_bytes("x/y.txt") == b"hello"
    assert s.describe("x/y.txt") == "s3://b/prod/x/y.txt"


def test_faiss_memory_on_s3(aws, config):
    boto3.client("s3").create_bucket(Bucket="b")
    storage = S3Storage("b", "prod")
    embedder = HashEmbedder()
    memory = StoryMemory.load(storage, embedder.dim)
    assert memory.size == 0
    result = Editor(config.tuning.editor, embedder).run(
        [make_article("Bank merger announced"), make_article("Chip rules tightened")], memory
    )
    remember(memory, result.clusters, result)
    memory.save(storage, "run-1")
    assert StoryMemory.load(storage, embedder.dim).size == 2


def test_ses_sender(aws):
    ses = boto3.client("sesv2")
    ses.create_email_identity(EmailIdentity="briefing@example.com")
    sender = SESSender("briefing@example.com", ["you@example.com"], client=ses)
    md = "# Daily Briefing\n\n## Bottom line\nHello"
    assert sender.send("Subject", md, render_html(md))
