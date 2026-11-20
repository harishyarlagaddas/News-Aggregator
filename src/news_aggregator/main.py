"""CLI entry point.

  news-agg run              full pipeline: fetch, dedupe, write, email, persist
  news-agg run --dry-run    full pipeline, writes out/DATE.md|html; no email, no state changes
  news-agg fetch            fetch only and print per-source counts (feed health check)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

from .config import load_config
from .logging_setup import configure_logging, log


def _build_deps(args, config):
    from .delivery import NullSender, build_sender
    from .editor import build_embedder, build_judge
    from .fetcher import Fetcher
    from .graph import Deps
    from .priority import DeepScraper
    from .storage import build_storage
    from .writer import CrewWriter

    return Deps(
        config=config,
        storage=build_storage(config),
        fetcher=Fetcher(config),
        embedder=build_embedder(config),
        scraper=DeepScraper(config.tuning.fetch, config.tuning.priority),
        writer=CrewWriter(config.tuning.writer, config.llm_model),
        sender=NullSender() if args.dry_run else build_sender(config),
        judge=build_judge(config.tuning.dedupe, config.env.aws_region),
        dry_run=args.dry_run,
        output_dir=Path(args.output_dir),
    )


def cmd_run(args) -> int:
    from .graph import run_pipeline

    config = load_config(args.config_dir)
    configure_logging(config.env.log_format)
    try:
        final = asyncio.run(run_pipeline(_build_deps(args, config), args.date))
    except Exception:
        log.exception("run_failed")
        return 1
    return 0 if (final.get("sent") or args.dry_run) else 1


def cmd_fetch(args) -> int:
    from .fetcher import Fetcher

    config = load_config(args.config_dir)
    configure_logging(config.env.log_format)
    result = asyncio.run(Fetcher(config).fetch_all({}))
    counts = Counter(a.source for a in result.articles)
    for source in config.enabled_sources:
        status = "FAILED" if source.name in result.failed_sources else counts.get(source.name, 0)
        print(f"{source.name:<32} {status}")
    print(f"\nTotal fresh items: {len(result.articles)}")
    return 0 if not result.failed_sources else 2


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="news-agg")
    parser.add_argument("--config-dir", default=None, help="directory with sources/settings.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the full pipeline")
    run.add_argument("--dry-run", action="store_true", help="no email, no state changes")
    run.add_argument("--date", default=None, help="briefing date label (YYYY-MM-DD)")
    run.add_argument("--output-dir", default="out", help="dry-run output directory")
    run.set_defaults(func=cmd_run)

    fetch = sub.add_parser("fetch", help="fetch sources and print counts")
    fetch.set_defaults(func=cmd_fetch)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(cli())
