"""LangGraph orchestration.

fetch -> edit -> score -> llm_dedupe -> (deep_scrape | skip) -> write -> validate -> (send | rewrite | fallback)
      -> send -> persist

Nothing is persisted until the email has been sent: if any step fails, the S3 index, the
HTTP cache and the archive are untouched, so re-running the task reproduces the briefing.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .config import AppConfig
from .delivery import EmailSender, render_html
from .editor import (
    DuplicateJudge,
    Editor,
    EditorResult,
    Embedder,
    StoryMemory,
    merge_duplicates,
    remember,
)
from .fetcher import FetchResult
from .logging_setup import log
from .models import PipelineState, RunStats, StoryCluster, StoryStatus, utcnow
from .priority import PriorityScorer
from .storage import Storage
from .writer import (
    BriefingInput,
    BriefingWriter,
    assemble,
    build_input,
    fallback_body,
    llm_payload,
    validate_briefing,
)

HTTP_CACHE_KEY = "state/http_cache.json"
MAX_WRITE_ATTEMPTS = 2


class FetcherLike(Protocol):
    async def fetch_all(self, http_cache: dict | None = None) -> FetchResult: ...


class ScraperLike(Protocol):
    async def run(self, clusters: list[StoryCluster]) -> tuple[int, int]: ...


@dataclass
class Deps:
    config: AppConfig
    storage: Storage
    fetcher: FetcherLike
    embedder: Embedder
    scraper: ScraperLike
    writer: BriefingWriter
    sender: EmailSender
    dry_run: bool = False
    output_dir: Path = Path("out")
    judge: DuplicateJudge | None = None   # None disables the LLM duplicate check


@dataclass
class RunContext:
    """Heavy, non-serialisable objects for one run (kept out of graph state)."""

    memory: StoryMemory | None = None
    editor_result: EditorResult | None = None
    http_cache: dict = field(default_factory=dict)
    ranked: list[StoryCluster] = field(default_factory=list)
    briefing_input: BriefingInput | None = None
    deep_ids: list[str] = field(default_factory=list)
    scorer: PriorityScorer | None = None
    # surviving cluster_id -> stories the LLM duplicate check folded into it
    absorbed: dict[str, list[StoryCluster]] = field(default_factory=dict)


def build_graph(deps: Deps):
    cfg = deps.config
    ctx = RunContext()

    # -- nodes ----------------------------------------------------------------
    async def fetch(state: PipelineState) -> dict[str, Any]:
        raw = deps.storage.read_bytes(HTTP_CACHE_KEY)
        ctx.http_cache = json.loads(raw) if raw else {}
        result = await deps.fetcher.fetch_all(ctx.http_cache)
        ctx.http_cache = result.http_cache
        stats = state["stats"]
        stats.sources_total = len(cfg.enabled_sources)
        stats.sources_failed = result.failed_sources
        stats.items_fetched = len(result.articles)
        log.info("fetch_done", items=len(result.articles), failed=len(result.failed_sources))
        return {
            "raw_articles": result.articles,
            "source_errors": result.failed_sources,
            "stats": stats,
        }

    async def edit(state: PipelineState) -> dict[str, Any]:
        ctx.memory = await asyncio.to_thread(StoryMemory.load, deps.storage, deps.embedder.dim)
        editor = Editor(cfg.tuning.editor, deps.embedder)
        result = await asyncio.to_thread(editor.run, state["raw_articles"], ctx.memory)
        ctx.editor_result = result
        stats = state["stats"]
        stats.items_after_exact_dedupe = result.items_after_exact
        stats.unique_stories = len(result.clusters)
        stats.repeats_suppressed = result.repeats
        log.info("edit_done", stories=len(result.clusters), repeats=result.repeats)
        return {"clusters": result.clusters, "stats": stats}

    async def score(state: PipelineState) -> dict[str, Any]:
        assert ctx.editor_result is not None
        watch = cfg.tuning.priority.watchlist
        watch_vecs = await asyncio.to_thread(deps.embedder.embed, watch) if watch else None
        scorer = ctx.scorer = PriorityScorer(cfg.tuning.priority, watch_vecs)
        ctx.ranked = scorer.score(state["clusters"], ctx.editor_result.centroids)
        top_ids, deep_ids = scorer.select(ctx.ranked)
        ctx.deep_ids = deep_ids
        log.info("score_done", top=len(top_ids), deep_scrape=len(deep_ids))
        return {"priority_ids": top_ids}

    async def llm_dedupe(state: PipelineState) -> dict[str, Any]:
        """Ask a reasoning model which top-ranked stories are the same event, and merge them."""
        if deps.judge is None or len(ctx.ranked) < 2:
            return {}
        assert ctx.editor_result is not None and ctx.scorer is not None
        candidates = ctx.ranked[: cfg.tuning.dedupe.max_candidates]
        try:
            groups = await asyncio.to_thread(deps.judge.find_duplicates, candidates)
        except Exception as exc:  # noqa: BLE001 - optional step: carry on without merging
            log.warning("llm_dedupe_failed", error=repr(exc)[:300])
            return {}

        remaining, absorbed = merge_duplicates(ctx.ranked, groups)
        merged = sum(len(dupes) for dupes in absorbed.values())
        log.info("llm_dedupe_done", reviewed=len(candidates), merged=merged)
        if not merged:
            return {}
        for keeper in remaining:
            for dupe in absorbed.get(keeper.cluster_id, []):
                log.info("llm_dedupe_merge", kept=keeper.representative.title,
                         absorbed=dupe.representative.title)

        # Merged stories have more sources, so re-rank and re-pick the top stories.
        ctx.absorbed = absorbed
        ctx.ranked = ctx.scorer.score(remaining, ctx.editor_result.centroids)
        top_ids, ctx.deep_ids = ctx.scorer.select(ctx.ranked)
        stats = state["stats"]
        stats.llm_duplicates_merged = merged
        stats.unique_stories -= merged
        return {"priority_ids": top_ids, "stats": stats}

    async def deep_scrape(state: PipelineState) -> dict[str, Any]:
        wanted = set(ctx.deep_ids)
        targets = [c for c in ctx.ranked if c.cluster_id in wanted]
        ok, failed = await deps.scraper.run(targets)
        stats = state["stats"]
        stats.deep_scraped, stats.deep_scrape_failed = ok, failed
        log.info("deep_scrape_done", ok=ok, failed=failed)
        return {"stats": stats}

    async def write(state: PipelineState) -> dict[str, Any]:
        attempts = state.get("attempts", 0) + 1
        ctx.briefing_input = build_input(
            state["run_date"],
            ctx.ranked,
            state["priority_ids"],
            state["stats"],
            cfg.tuning.writer.max_theme_items,
        )
        if not ctx.briefing_input.top:
            log.info("write_skipped", reason="no new stories")
            return {"attempts": MAX_WRITE_ATTEMPTS, "briefing_md": None,
                    "validation_errors": ["no new stories"]}

        inputs = llm_payload(ctx.briefing_input)
        if errors := state.get("validation_errors"):
            inputs["feedback"] = "; ".join(errors)
        try:
            body = await asyncio.to_thread(deps.writer.write, inputs)
        except Exception as exc:  # noqa: BLE001 - LLM/network failure: retry or fall back
            log.warning("writer_failed", attempt=attempts, error=repr(exc)[:300])
            return {"attempts": attempts, "briefing_md": None,
                    "validation_errors": [f"writer error: {exc!r}"[:300]]}
        return {"attempts": attempts, "briefing_md": assemble(ctx.briefing_input, body)}

    def validate(state: PipelineState) -> dict[str, Any]:
        md = state.get("briefing_md")
        if md is None:
            return {}  # errors already recorded by write
        assert ctx.briefing_input is not None
        w = cfg.tuning.writer
        errors = validate_briefing(
            md,
            ctx.briefing_input.allowed_urls,
            expected_top=len(ctx.briefing_input.top),
            min_words=w.min_words,
            max_words=w.max_words,
        )
        if errors:
            log.warning("validation_failed", attempt=state["attempts"], errors=errors)
        return {"validation_errors": errors}

    def fallback(state: PipelineState) -> dict[str, Any]:
        assert ctx.briefing_input is not None
        stats = state["stats"]
        stats.used_fallback_digest = bool(ctx.briefing_input.top)
        log.warning("using_fallback_digest", errors=state.get("validation_errors"))
        return {"briefing_md": assemble(ctx.briefing_input, fallback_body(ctx.briefing_input)),
                "stats": stats}

    def send(state: PipelineState) -> dict[str, Any]:
        md = state["briefing_md"] or ""
        subject = cfg.tuning.email.subject_template.format(date=state["run_date"])
        if deps.dry_run:
            log.info("dry_run_email_skipped", subject=subject)
            return {"sent": False}
        message_id = deps.sender.send(subject, md, render_html(md))
        # CloudWatch metric filter keys off this event name. Do not rename.
        log.info("briefing_sent", subject=subject, message_id=message_id,
                 recipients=len(cfg.recipients))
        return {"sent": True}

    def persist(state: PipelineState) -> dict[str, Any]:
        md = state["briefing_md"] or ""
        date = state["run_date"]
        if deps.dry_run:
            deps.output_dir.mkdir(parents=True, exist_ok=True)
            path = deps.output_dir / f"{date}.md"
            path.write_text(md)
            (deps.output_dir / f"{date}.html").write_text(render_html(md))
            log.info("dry_run_written", path=str(path))
            return {}

        deps.storage.write_bytes(f"briefings/{date}.md", md.encode(), "text/markdown")
        assert ctx.memory is not None and ctx.editor_result is not None
        reported = ctx.briefing_input.reported if ctx.briefing_input else []
        # Repeats are refreshed too, so ongoing stories stay suppressed while still covered.
        repeats = [c for c in state["clusters"] if c.status == StoryStatus.REPEAT]
        # Stories folded into a reported one were reported too: remember their wording so
        # tomorrow's coverage of the same event is recognised whichever way it is phrased.
        reported_ids = {c.cluster_id for c in reported}
        absorbed = [d for cid, dupes in ctx.absorbed.items() if cid in reported_ids for d in dupes]
        remember(ctx.memory, reported + repeats + absorbed, ctx.editor_result)
        pruned = ctx.memory.prune(utcnow() - timedelta(days=cfg.tuning.editor.memory_days))
        ctx.memory.save(deps.storage, state["run_id"])
        deps.storage.write_bytes(
            HTTP_CACHE_KEY, json.dumps(ctx.http_cache).encode(), "application/json"
        )
        log.info("persist_done", remembered=len(reported) + len(repeats) + len(absorbed),
                 pruned=pruned)
        return {}

    # -- routing --------------------------------------------------------------
    def after_dedupe(state: PipelineState) -> str:
        return "deep_scrape" if ctx.deep_ids else "write"

    def after_validate(state: PipelineState) -> str:
        if state.get("briefing_md") and not state.get("validation_errors"):
            return "send"
        if state.get("attempts", 0) < MAX_WRITE_ATTEMPTS:
            return "write"
        return "fallback"

    # -- wiring ---------------------------------------------------------------
    g = StateGraph(PipelineState)
    for name, fn in [
        ("fetch", fetch), ("edit", edit), ("score", score), ("llm_dedupe", llm_dedupe),
        ("deep_scrape", deep_scrape),
        ("write", write), ("validate", validate), ("fallback", fallback),
        ("send", send), ("persist", persist),
    ]:
        g.add_node(name, fn)

    g.add_edge(START, "fetch")
    g.add_edge("fetch", "edit")
    g.add_edge("edit", "score")
    g.add_edge("score", "llm_dedupe")
    g.add_conditional_edges(
        "llm_dedupe", after_dedupe, {"deep_scrape": "deep_scrape", "write": "write"}
    )
    g.add_edge("deep_scrape", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges(
        "validate", after_validate, {"send": "send", "write": "write", "fallback": "fallback"}
    )
    g.add_edge("fallback", "send")
    g.add_edge("send", "persist")
    g.add_edge("persist", END)

    return g.compile(checkpointer=MemorySaver())


async def run_pipeline(deps: Deps, run_date: str | None = None) -> PipelineState:
    started = utcnow()
    run_date = run_date or started.date().isoformat()
    run_id = f"{run_date}-{started.strftime('%H%M%S')}"
    graph = build_graph(deps)
    initial: PipelineState = {
        "run_id": run_id,
        "run_date": run_date,
        "started_at": started,
        "attempts": 0,
        "validation_errors": [],
        "stats": RunStats(),
        "sent": False,
    }
    log.info("run_started", run_id=run_id, dry_run=deps.dry_run)
    final = await graph.ainvoke(
        initial, config={"configurable": {"thread_id": run_id}, "recursion_limit": 25}
    )
    elapsed = (utcnow() - started).total_seconds()
    log.info("run_finished", run_id=run_id, seconds=round(elapsed, 1),
             stats=final["stats"].model_dump())
    return final
