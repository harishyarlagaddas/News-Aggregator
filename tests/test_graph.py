"""End-to-end pipeline with fakes for the network, LLM and email."""

from __future__ import annotations

from news_aggregator.editor.embed import HashEmbedder
from news_aggregator.fetcher import FetchResult
from news_aggregator.graph import Deps, run_pipeline
from news_aggregator.storage import LocalStorage

from .conftest import make_article

FED = "Federal Reserve holds interest rates steady as inflation cools"
CHIP = "Nvidia unveils new data center chip for artificial intelligence workloads"


class FakeFetcher:
    def __init__(self, articles, failed=None):
        self.articles, self.failed = articles, failed or []

    async def fetch_all(self, http_cache=None):
        return FetchResult(self.articles, self.failed, {"feed": {"etag": "abc"}})


class FakeScraper:
    async def run(self, clusters):
        for c in clusters:
            c.full_text = "Full article text. " * 50
            c.text_status = c.text_status.FULL_TEXT
        return len(clusters), 0


class FakeWriter:
    """Writes a valid body from the payload; optionally fails the first N calls."""

    def __init__(self, bad_first: int = 0, always_bad: bool = False):
        self.calls, self.bad_first, self.always_bad = 0, bad_first, always_bad

    def write(self, inputs):
        import json

        self.calls += 1
        if self.always_bad or self.calls <= self.bad_first:
            return "## Bottom line\nToo short [x](https://invented.example.com)"
        stories = json.loads(inputs["stories_json"])
        out = ["## Bottom line", "Rates and chips dominate. " * 12, "", "## Top stories"]
        for s in stories:
            link = s["links"][0]
            out += [f"### {s['rank']}. {s['headline']}", "**What happened:** Detail. " * 6,
                    f"Sources: [{link['source']}]({link['url']})", ""]
        return "\n".join(out)


class FakeSender:
    def __init__(self):
        self.sent = []

    def send(self, subject, markdown_text, html):
        self.sent.append((subject, markdown_text, html))
        return "msg-1"


def _articles():
    return [
        make_article(FED, "Outlet A", weight=1.3),
        make_article(FED + " - Outlet B", "Outlet B"),
        make_article(CHIP, "Outlet C"),
    ]


def _deps(config, tmp_path, writer, articles=None, dry_run=False):
    config.tuning.writer.min_words = 30
    config.tuning.priority.min_score_for_deep_scrape = 0.0
    return Deps(
        config=config,
        storage=LocalStorage(tmp_path / "store"),
        fetcher=FakeFetcher(articles or _articles(), failed=["Broken Feed"]),
        embedder=HashEmbedder(),
        scraper=FakeScraper(),
        writer=writer,
        sender=FakeSender(),
        dry_run=dry_run,
        output_dir=tmp_path / "out",
    )


async def test_full_run_sends_and_persists(config, tmp_path):
    deps = _deps(config, tmp_path, FakeWriter())
    final = await run_pipeline(deps, "2026-11-16")

    assert final["sent"] is True
    subject, md, html = deps.sender.sent[0]
    assert subject == "Daily Briefing: 2026-11-16"
    assert "## Top stories" in md and "<h2" in html
    assert "1 sources failed (Broken Feed)" in md
    assert final["stats"].deep_scraped == 2
    store = tmp_path / "store"
    assert (store / "briefings/2026-11-16.md").exists()
    assert (store / "index/index.faiss").exists()
    assert (store / "index/metadata.parquet").exists()
    assert (store / "state/http_cache.json").exists()


async def test_second_day_suppresses_repeats(config, tmp_path):
    await run_pipeline(_deps(config, tmp_path, FakeWriter()), "2026-11-16")
    deps = _deps(config, tmp_path, FakeWriter())
    final = await run_pipeline(deps, "2026-11-17")
    assert final["stats"].repeats_suppressed == 2
    # Nothing new: a short "no new stories" digest is still sent.
    assert final["sent"] is True


async def test_rewrite_then_success(config, tmp_path):
    writer = FakeWriter(bad_first=1)
    final = await run_pipeline(_deps(config, tmp_path, writer), "2026-11-16")
    assert writer.calls == 2
    assert final["stats"].used_fallback_digest is False


async def test_fallback_after_two_bad_drafts(config, tmp_path):
    writer = FakeWriter(always_bad=True)
    deps = _deps(config, tmp_path, writer)
    final = await run_pipeline(deps, "2026-11-16")
    assert writer.calls == 2
    assert final["stats"].used_fallback_digest is True
    assert "fallback digest" in deps.sender.sent[0][1]


async def test_dry_run_writes_files_but_no_state(config, tmp_path):
    deps = _deps(config, tmp_path, FakeWriter(), dry_run=True)
    final = await run_pipeline(deps, "2026-11-16")
    assert final["sent"] is False and deps.sender.sent == []
    assert (tmp_path / "out/2026-11-16.md").exists()
    assert not (tmp_path / "store/index/index.faiss").exists()
