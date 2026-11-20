"""Deterministic checks on the briefing before it is emailed."""

from __future__ import annotations

import re

from ..fetcher.canonical import canonicalize_url
from .digest import BOTTOM_HEADING, TOP_HEADING

MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^\s)]+)\)")
BARE_URL_RE = re.compile(r"(?<![(\[])\bhttps?://[^\s)>\]]+")
STORY_HEADING_RE = re.compile(r"^###\s+\d+\.", re.MULTILINE)
TEMPLATE_RESIDUE_RE = re.compile(r"\{[a-z_]+\}")


def extract_urls(markdown: str) -> list[str]:
    urls = [u for _, u in MD_LINK_RE.findall(markdown)]
    stripped = MD_LINK_RE.sub("", markdown)
    urls += BARE_URL_RE.findall(stripped)
    return urls


def validate_briefing(
    markdown: str,
    allowed_urls: set[str],
    *,
    expected_top: int,
    min_words: int,
    max_words: int,
) -> list[str]:
    """Returns a list of problems; empty means the briefing can be sent."""
    errors: list[str] = []

    for heading in (BOTTOM_HEADING, TOP_HEADING):
        if heading not in markdown:
            errors.append(f"missing section '{heading}'")

    allowed = {canonicalize_url(u) for u in allowed_urls}
    unknown = sorted({u for u in extract_urls(markdown) if canonicalize_url(u) not in allowed})
    if unknown:
        errors.append(f"{len(unknown)} link(s) not in the source set: {unknown[:5]}")

    top_section = ""
    if TOP_HEADING in markdown:
        top_section = re.split(r"^## ", markdown.split(TOP_HEADING, 1)[1], maxsplit=1,
                               flags=re.MULTILINE)[0]
    stories = len(STORY_HEADING_RE.findall(top_section))
    if expected_top and stories == 0:
        errors.append("no numbered top stories found")
    if stories > expected_top:
        errors.append(f"too many top stories ({stories} > {expected_top})")

    words = len(markdown.split())
    if words < min_words:
        errors.append(f"too short ({words} words < {min_words})")
    if words > max_words:
        errors.append(f"too long ({words} words > {max_words})")

    if residue := TEMPLATE_RESIDUE_RE.findall(markdown):
        errors.append(f"unfilled template placeholders: {residue[:3]}")

    return errors
