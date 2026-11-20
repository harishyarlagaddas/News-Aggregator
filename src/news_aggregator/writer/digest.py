"""Deterministic parts of the briefing: the LLM payload, final assembly, and the fallback digest.

The LLM writes only "Bottom line", "Top stories" and "Developing". Theme one-liners and
run stats are rendered from data, so they can never be hallucinated.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..models import RunStats, StoryCluster, StoryStatus, TextStatus

TOP_HEADING = "## Top stories"
BOTTOM_HEADING = "## Bottom line"
DEVELOPING_HEADING = "## Developing"
THEME_HEADING = "## By theme"


@dataclass
class BriefingInput:
    date: str
    top: list[StoryCluster]
    developing: list[StoryCluster]
    themes: list[StoryCluster]
    stats: RunStats
    allowed_urls: set[str] = field(default_factory=set)

    @property
    def reported(self) -> list[StoryCluster]:
        seen: dict[str, StoryCluster] = {}
        for c in [*self.top, *self.developing, *self.themes]:
            seen.setdefault(c.cluster_id, c)
        return list(seen.values())


def build_input(
    date: str,
    ranked: list[StoryCluster],
    top_ids: list[str],
    stats: RunStats,
    max_theme_items: int,
) -> BriefingInput:
    top_set = set(top_ids)
    top = [c for c in ranked if c.cluster_id in top_set]
    developing = [
        c for c in ranked if c.status == StoryStatus.DEVELOPING and c.cluster_id not in top_set
    ][:5]
    used = top_set | {c.cluster_id for c in developing}
    themes = [c for c in ranked if c.cluster_id not in used][:max_theme_items]
    allowed = {url for c in [*top, *developing, *themes] for _, url in c.member_links}
    return BriefingInput(date, top, developing, themes, stats, allowed)


def _story_payload(rank: int, c: StoryCluster) -> dict:
    text = c.full_text if c.text_status == TextStatus.FULL_TEXT else None
    return {
        "rank": rank,
        "headline": c.representative.title,
        "category": c.representative.category,
        "text_status": c.text_status.value,
        "status": c.status.value,
        "source_count": c.source_count,
        "previous_source_count": c.previous_source_count,
        "summaries": [
            {"source": m.source, "title": m.title, "summary": m.summary}
            for m in c.members[:6]
        ],
        "full_text": text,
        "links": [{"source": s, "url": u} for s, u in c.member_links[:6]],
    }


def llm_payload(inp: BriefingInput) -> dict[str, str]:
    """Inputs for the CrewAI crew (values are JSON strings, interpolated into task prompts)."""
    return {
        "date": inp.date,
        "story_count": str(len(inp.top)),
        "stories_json": json.dumps(
            [_story_payload(i + 1, c) for i, c in enumerate(inp.top)], ensure_ascii=False
        ),
        "developing_json": json.dumps(
            [_story_payload(i + 1, c) for i, c in enumerate(inp.developing)], ensure_ascii=False
        ),
    }


def _links(c: StoryCluster, limit: int = 2) -> str:
    links = c.member_links
    shown = " · ".join(f"[{s}]({u})" for s, u in links[:limit])
    more = f" (+{len(links) - limit} more)" if len(links) > limit else ""
    return shown + more


def render_themes(themes: list[StoryCluster]) -> str:
    if not themes:
        return ""
    groups: dict[str, list[StoryCluster]] = defaultdict(list)
    for c in themes:
        groups[c.representative.category or "Other"].append(c)
    lines = [THEME_HEADING]
    for category in sorted(groups):
        lines.append(f"\n### {category}")
        for c in groups[category]:
            lines.append(f"- {c.representative.title} ({_links(c, 1)})")
    return "\n".join(lines)


def render_footer(stats: RunStats) -> str:
    parts = [
        f"{stats.sources_total} sources",
        f"{stats.items_fetched:,} items",
        f"{stats.unique_stories:,} unique stories",
        f"{stats.repeats_suppressed} repeats suppressed",
        f"{stats.deep_scraped} full-text reads",
    ]
    if stats.llm_duplicates_merged:
        parts.append(f"{stats.llm_duplicates_merged} duplicates merged by AI check")
    if stats.sources_failed:
        parts.append(f"{len(stats.sources_failed)} sources failed ({', '.join(stats.sources_failed)})")
    if stats.used_fallback_digest:
        parts.append("fallback digest (AI summary unavailable)")
    return "---\n_Run stats: " + " · ".join(parts) + "_"


FIELD_RE = re.compile(
    r"\s*(?:[-*][ \t]+)?\*{0,2}(What happened|Why it matters|What to watch)\*{0,2}[ \t]*:"
    r"[ \t]*\*{0,2}[ \t]*"
)
SOURCES_RE = re.compile(r"\s*(?:[-*][ \t]+)?\*{0,2}Sources?\*{0,2}[ \t]*:[ \t]*\*{0,2}[ \t]*")


def tidy_story_layout(body: str) -> str:
    """Put each story field and the Sources line in its own paragraph.

    Markdown joins consecutive lines into one paragraph, and models sometimes write a whole
    story on one line, so the email showed each story as a single block of text.
    """
    body = FIELD_RE.sub(lambda m: f"\n\n**{m.group(1)}:** ", body)
    body = SOURCES_RE.sub("\n\nSources: ", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def assemble(inp: BriefingInput, llm_body: str) -> str:
    """Header + LLM sections, with 'By theme' inserted before 'Developing', + footer."""
    body = llm_body.strip()
    # Drop any title the model added; we render our own.
    if body.startswith("# "):
        body = body.split("\n", 1)[1].strip() if "\n" in body else ""
    body = tidy_story_layout(body)
    themes = render_themes(inp.themes)
    if DEVELOPING_HEADING in body and themes:
        before, after = body.split(DEVELOPING_HEADING, 1)
        body = f"{before.rstrip()}\n\n{themes}\n\n{DEVELOPING_HEADING}{after}"
    elif themes:
        body = f"{body}\n\n{themes}"
    return f"# Daily Briefing: {inp.date}\n\n{body}\n\n{render_footer(inp.stats)}\n"


def fallback_body(inp: BriefingInput) -> str:
    """Ranked headlines and links, used when the AI writer fails validation twice."""
    lines = [
        BOTTOM_HEADING,
        "The AI summary could not be produced today. Below are the highest-ranked stories "
        "by coverage, source authority and recency.",
        "",
        TOP_HEADING,
    ]
    for i, c in enumerate(inp.top, 1):
        lines.append(f"\n### {i}. {c.representative.title}")
        if c.representative.summary:
            lines.append(c.representative.summary)
        lines.append(f"Sources: {_links(c, 3)}")
    if inp.developing:
        lines.append(f"\n{DEVELOPING_HEADING}")
        for c in inp.developing:
            lines.append(f"- {c.representative.title} ({_links(c, 1)})")
    return "\n".join(lines)
