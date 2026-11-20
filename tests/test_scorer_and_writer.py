from news_aggregator.editor import Editor, StoryMemory
from news_aggregator.editor.embed import HashEmbedder
from news_aggregator.models import RunStats, TextStatus
from news_aggregator.priority import PriorityScorer
from news_aggregator.writer import assemble, build_input, fallback_body, validate_briefing
from news_aggregator.writer.digest import llm_payload

from .conftest import make_article


def _ranked(config):
    arts = [
        make_article("Big merger announced between two banks", "A", weight=1.3, hours_ago=1),
        make_article("Big merger announced between two banks - B", "B", hours_ago=2),
        make_article("Big merger announced between two banks - C", "C", hours_ago=2),
        make_article("Small startup raises seed round", "D", weight=0.7, hours_ago=30,
                     category="Technology"),
        make_article("Chip export rules tightened", "E", hours_ago=3, category="Technology"),
    ]
    embedder = HashEmbedder()
    result = Editor(config.tuning.editor, embedder).run(arts, StoryMemory(embedder.dim))
    scorer = PriorityScorer(config.tuning.priority, embedder.embed(["bank mergers"]))
    return scorer, scorer.score(result.clusters, result.centroids), result


def test_scorer_ranks_widely_covered_recent_story_first(config):
    _, ranked, _ = _ranked(config)
    assert ranked[0].source_count == 3
    assert ranked[-1].representative.title.startswith("Small startup")
    assert all(0 <= c.score <= 1 for c in ranked)
    assert set(ranked[0].score_breakdown) == {"coverage", "authority", "recency", "relevance"}


def test_select_respects_top_n_and_deep_scrape_threshold(config):
    config.tuning.priority.top_n = 2
    config.tuning.priority.min_score_for_deep_scrape = 0.0
    scorer, ranked, _ = _ranked(config)
    top, deep = scorer.select(ranked)
    assert len(top) == 2 and deep == top


def _good_body(inp):
    lines = ["## Bottom line", "A bank merger leads the day. " * 10, "", "## Top stories"]
    for i, c in enumerate(inp.top, 1):
        src, url = c.member_links[0]
        lines += [
            f"### {i}. {c.representative.title}",
            "**What happened:** Something. " * 5,
            "**Why it matters:** Impact. " * 5,
            "**What to watch:** Next steps.",
            f"Sources: [{src}]({url})",
            "",
        ]
    return "\n".join(lines)


def test_assemble_and_validate_good_briefing(config):
    config.tuning.priority.top_n = 2
    scorer, ranked, _ = _ranked(config)
    top, _ = scorer.select(ranked)
    inp = build_input("2026-11-16", ranked, top, RunStats(sources_total=5), max_theme_items=10)
    md = assemble(inp, _good_body(inp))
    assert md.startswith("# Daily Briefing: 2026-11-16")
    assert "## By theme" in md and "_Run stats:" in md
    errors = validate_briefing(md, inp.allowed_urls, expected_top=2, min_words=50, max_words=3000)
    assert errors == []


def test_validate_rejects_invented_links_and_missing_sections(config):
    scorer, ranked, _ = _ranked(config)
    top, _ = scorer.select(ranked)
    inp = build_input("2026-11-16", ranked, top, RunStats(), max_theme_items=10)
    md = "## Top stories\n### 1. X\nSources: [Fake](https://invented.example.com/a)\n"
    errors = validate_briefing(md, inp.allowed_urls, expected_top=len(top), min_words=1,
                               max_words=3000)
    assert any("Bottom line" in e for e in errors)
    assert any("not in the source set" in e for e in errors)


def test_fallback_digest_passes_validation(config):
    scorer, ranked, _ = _ranked(config)
    top, _ = scorer.select(ranked)
    inp = build_input("2026-11-16", ranked, top, RunStats(), max_theme_items=10)
    md = assemble(inp, fallback_body(inp))
    assert validate_briefing(md, inp.allowed_urls, expected_top=len(top), min_words=10,
                             max_words=3000) == []


def test_llm_payload_only_includes_full_text_when_scraped(config):
    scorer, ranked, _ = _ranked(config)
    top, _ = scorer.select(ranked)
    ranked[0].full_text, ranked[0].text_status = "Full article body", TextStatus.FULL_TEXT
    inp = build_input("2026-11-16", ranked, top, RunStats(), max_theme_items=10)
    payload = llm_payload(inp)
    assert "Full article body" in payload["stories_json"]
    assert payload["story_count"] == str(len(inp.top))


def test_story_fields_get_their_own_paragraphs():
    from news_aggregator.delivery import render_html
    from news_aggregator.writer.digest import tidy_story_layout

    body = (
        "## Top stories\n### 1. Anthropic IPO\n"
        "**What happened:** A. **Why it matters:** B.\n"
        "**What to watch**: C. Sources: [G](https://g.com/a)\n\n"
        "### 2. Fed\n- What happened: X\n- Why it matters: Y\n**Sources:** [G](https://g.com/b)"
    )
    tidy = tidy_story_layout(body)
    assert "\n\n**What happened:** A.\n\n**Why it matters:** B.\n\n**What to watch:** C." in tidy
    assert "\n\nSources: [G](https://g.com/a)\n\n### 2. Fed\n\n**What happened:** X" in tidy
    assert tidy_story_layout(tidy) == tidy
    html = render_html(tidy)
    assert html.count("<p") == 7  # 3 + 1 for story 1, 2 + 1 for story 2
