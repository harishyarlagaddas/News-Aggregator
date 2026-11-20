"""On-demand full-text extraction for high-priority stories only.

For each story, member URLs are tried from highest to lowest authority until one yields
clean article text. trafilatura handles normal pages; Playwright is a fallback for
JavaScript-rendered pages. Paywalls and access controls are never bypassed: if no
member yields text, the story stays ``summary_only``.
"""

from __future__ import annotations

import asyncio

import trafilatura

from ..config import FetchConfig, PriorityConfig
from ..fetcher.http import BlockedByRobots, PoliteClient
from ..logging_setup import log
from ..models import StoryCluster, TextStatus

MIN_ARTICLE_CHARS = 600
MAX_URLS_PER_STORY = 3


def extract_text(html: str, url: str) -> str | None:
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
        deduplicate=True,
    )
    if text and len(text) >= MIN_ARTICLE_CHARS:
        return text
    return None


class BrowserFetcher:
    """Lazily started headless Chromium, shared across one run."""

    def __init__(self, user_agent: str, timeout_ms: int = 20000):
        self.user_agent = user_agent
        self.timeout_ms = timeout_ms
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        async with self._lock:
            if self._browser is None:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(args=["--no-sandbox"])
        return self._browser

    async def html(self, url: str) -> str:
        browser = await self._ensure()
        page = await browser.new_page(user_agent=self.user_agent)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            await page.wait_for_timeout(1500)
            return await page.content()
        finally:
            await page.close()

    async def close(self):
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()


class DeepScraper:
    def __init__(self, fetch_cfg: FetchConfig, cfg: PriorityConfig):
        self.fetch_cfg = fetch_cfg
        self.cfg = cfg

    async def _story(self, cluster: StoryCluster, http: PoliteClient, browser) -> bool:
        for _, url in cluster.member_links[:MAX_URLS_PER_STORY]:
            text = None
            try:
                resp = await http.get(url)  # respects robots.txt
                text = await asyncio.to_thread(extract_text, resp.text, url)
            except BlockedByRobots:
                log.info("deep_scrape_robots_blocked", url=url)
                continue
            except Exception as exc:  # noqa: BLE001 - any failure falls through to next URL
                log.info("deep_scrape_http_failed", url=url, error=repr(exc)[:200])

            if text is None and browser is not None and await http.allowed(url):
                try:
                    html = await browser.html(url)
                    text = await asyncio.to_thread(extract_text, html, url)
                except Exception as exc:  # noqa: BLE001
                    log.info("deep_scrape_browser_failed", url=url, error=repr(exc)[:200])

            if text:
                cluster.full_text = text[: self.cfg.max_chars_per_story]
                cluster.text_status = TextStatus.FULL_TEXT
                return True
        cluster.text_status = TextStatus.SUMMARY_ONLY
        return False

    async def run(self, clusters: list[StoryCluster]) -> tuple[int, int]:
        """Scrape in place. Returns (succeeded, failed)."""
        if not clusters:
            return 0, 0
        browser = None
        if self.cfg.use_browser_fallback:
            try:
                import playwright  # noqa: F401

                browser = BrowserFetcher(self.fetch_cfg.user_agent)
            except ImportError:
                log.info("browser_fallback_unavailable")
        try:
            async with PoliteClient(self.fetch_cfg) as http:
                results = await asyncio.gather(
                    *(self._story(c, http, browser) for c in clusters), return_exceptions=True
                )
        finally:
            if browser:
                await browser.close()
        ok = sum(r is True for r in results)
        return ok, len(results) - ok
