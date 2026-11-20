"""LLM duplicate check: answer parsing, merging, and the pipeline step (with a fake judge)."""

from __future__ import annotations

import pytest

from news_aggregator.editor.judge import merge_duplicates, parse_groups, story_lines
from news_aggregator.models import StoryCluster, utcnow

from .conftest import make_article
from .test_graph import CHIP, FED, FakeWriter, _deps, run_pipeline

FED_REWORDED = "Powell keeps borrowing costs unchanged after price pressures ease"
OIL = "Oil prices jump after surprise production cut by exporters"


def _cluster(title: str, source: str = "Outlet A", weight: float = 1.0) -> StoryCluster:
    art = make_article(title, source, weight=weight, summary=f"Summary of {title}")
    return StoryCluster(
        cluster_id=art.id[:16], representative=art, members=[art],
        first_seen=art.published_at, last_seen=utcnow(),
    )


def test_story_lines_labels_candidates_in_order():
    text = story_lines([_cluster(FED), _cluster(CHIP, "Outlet C")])
    assert f"S1: {FED}" in text and f"S2: {CHIP}" in text
    assert "sources: Outlet C" in text


def test_parse_groups_maps_labels_and_drops_bad_entries():
    clusters = [_cluster(FED), _cluster(CHIP), _cluster(OIL)]
    answer = 'Here you go: {"duplicate_groups": [["S1", "S3", "S9"], ["S2"], "junk"]}'
    assert parse_groups(answer, clusters, 5) == [[clusters[0].cluster_id, clusters[2].cluster_id]]


def test_parse_groups_ignores_oversized_group():
    clusters = [_cluster(FED), _cluster(CHIP), _cluster(OIL)]
    assert parse_groups('{"duplicate_groups": [["S1", "S2", "S3"]]}', clusters, 2) == []


@pytest.mark.parametrize("answer", ["no json here", '{"groups": []}', '["S1", "S2"]'])
def test_parse_groups_rejects_malformed_answers(answer):
    with pytest.raises(ValueError):
        parse_groups(answer, [_cluster(FED), _cluster(CHIP)], 5)


def test_merge_keeps_best_ranked_and_combines_members():
    a, b, c = _cluster(FED), _cluster(CHIP, "Outlet C"), _cluster(FED_REWORDED, "Outlet B", 1.5)
    remaining, absorbed = merge_duplicates([a, b, c], [[c.cluster_id, a.cluster_id]])
    assert [x.cluster_id for x in remaining] == [a.cluster_id, b.cluster_id]
    assert absorbed == {a.cluster_id: [c]}
    assert a.sources == ["Outlet A", "Outlet B"]
    assert a.representative.title == FED_REWORDED  # highest-authority article leads


def test_merge_combines_overlapping_groups_and_ignores_unknown_ids():
    a, b, c = _cluster(FED), _cluster(FED_REWORDED, "Outlet B"), _cluster(OIL, "Outlet C")
    groups = [[a.cluster_id, b.cluster_id], [b.cluster_id, c.cluster_id, "missing"]]
    remaining, absorbed = merge_duplicates([a, b, c], groups)
    assert remaining == [a] and absorbed[a.cluster_id] == [b, c]


def test_merge_without_groups_is_a_no_op():
    ranked = [_cluster(FED), _cluster(CHIP)]
    assert merge_duplicates(ranked, []) == (ranked, {})


class FakeJudge:
    """Says the two Fed stories are one event."""

    def __init__(self, fail: bool = False):
        self.fail, self.seen = fail, 0

    def find_duplicates(self, clusters):
        self.seen = len(clusters)
        if self.fail:
            raise RuntimeError("model unavailable")
        fed = [c.cluster_id for c in clusters
               if c.representative.title in (FED, FED_REWORDED)]
        return [fed] if len(fed) > 1 else []


def _three_stories():
    return [
        make_article(FED, "Outlet A", weight=1.3),
        make_article(FED_REWORDED, "Outlet B"),
        make_article(CHIP, "Outlet C"),
    ]


async def test_pipeline_merges_llm_duplicates(config, tmp_path):
    deps = _deps(config, tmp_path, FakeWriter(), articles=_three_stories())
    deps.judge = FakeJudge()
    final = await run_pipeline(deps, "2026-11-16")

    assert deps.judge.seen == 3
    assert final["stats"].llm_duplicates_merged == 1
    assert final["stats"].unique_stories == 2
    assert len(final["priority_ids"]) == 2
    md = deps.sender.sent[0][1]
    assert md.count("\n### ") == 2 and FED_REWORDED not in md
    assert "1 duplicates merged by AI check" in md

    # Both wordings were remembered, so neither resurfaces the next day.
    deps2 = _deps(config, tmp_path, FakeWriter(), articles=_three_stories())
    final2 = await run_pipeline(deps2, "2026-11-17")
    assert final2["stats"].repeats_suppressed == 3


async def test_pipeline_survives_judge_failure(config, tmp_path):
    deps = _deps(config, tmp_path, FakeWriter(), articles=_three_stories())
    deps.judge = FakeJudge(fail=True)
    final = await run_pipeline(deps, "2026-11-16")
    assert final["sent"] is True
    assert final["stats"].llm_duplicates_merged == 0
    assert len(final["priority_ids"]) == 3
