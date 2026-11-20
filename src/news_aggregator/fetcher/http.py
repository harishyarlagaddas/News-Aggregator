"""Polite async HTTP: global + per-host concurrency limits, retries, robots.txt, conditional GET."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ..config import FetchConfig
from ..logging_setup import log


class NotModified(Exception):
    """Conditional GET returned 304; nothing new since the last run."""


class BlockedByRobots(Exception):
    pass


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 425, 429, 500, 502, 503, 504}
    return isinstance(exc, (httpx.TransportError, httpx.TimeoutException))


@dataclass
class Response:
    url: str
    status: int
    text: str
    headers: dict[str, str]


class PoliteClient:
    """Shared client for one run. Use as an async context manager."""

    def __init__(self, cfg: FetchConfig, http_cache: dict[str, dict[str, str]] | None = None):
        self.cfg = cfg
        self.http_cache = http_cache if http_cache is not None else {}
        self._global = asyncio.Semaphore(cfg.concurrency)
        self._per_host: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(cfg.per_host_concurrency)
        )
        self._robots: dict[str, RobotFileParser | None] = {}
        self._robots_lock = asyncio.Lock()
        self.client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "PoliteClient":
        self.client = httpx.AsyncClient(
            timeout=self.cfg.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self.cfg.user_agent, "Accept-Encoding": "gzip, deflate"},
            limits=httpx.Limits(max_connections=self.cfg.concurrency * 2),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self.client:
            await self.client.aclose()

    # -- robots.txt -----------------------------------------------------------
    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_lock:
            if origin not in self._robots:
                parser: RobotFileParser | None = RobotFileParser()
                try:
                    assert self.client is not None
                    resp = await self.client.get(f"{origin}/robots.txt")
                    if resp.status_code >= 400:
                        parser = None  # no robots.txt: everything allowed
                    else:
                        parser.parse(resp.text.splitlines())
                except httpx.HTTPError:
                    parser = None
                self._robots[origin] = parser
        parser = self._robots[origin]
        return True if parser is None else parser.can_fetch(self.cfg.user_agent, url)

    # -- GET ------------------------------------------------------------------
    async def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        conditional: bool = False,
        check_robots: bool = True,
        headers: dict[str, str] | None = None,
    ) -> Response:
        if check_robots and not await self.allowed(url):
            raise BlockedByRobots(url)

        req_headers = dict(headers or {})
        cache_key = url if not params else f"{url}?{sorted(params.items())}"
        if conditional and (cached := self.http_cache.get(cache_key)):
            if etag := cached.get("etag"):
                req_headers["If-None-Match"] = etag
            if modified := cached.get("last_modified"):
                req_headers["If-Modified-Since"] = modified

        host = urlsplit(url).netloc
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.cfg.max_retries),
            wait=wait_exponential_jitter(initial=1, max=10),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                async with self._global, self._per_host[host]:
                    assert self.client is not None
                    resp = await self.client.get(url, params=params, headers=req_headers)
                if resp.status_code == 304:
                    raise NotModified(url)
                resp.raise_for_status()

        if conditional:
            entry = {}
            if etag := resp.headers.get("ETag"):
                entry["etag"] = etag
            if modified := resp.headers.get("Last-Modified"):
                entry["last_modified"] = modified
            if entry:
                self.http_cache[cache_key] = entry

        log.debug("http_get", url=url, status=resp.status_code)
        return Response(str(resp.url), resp.status_code, resp.text, dict(resp.headers))
