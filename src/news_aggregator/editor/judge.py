"""LLM duplicate check: a reasoning model reviews the top-ranked stories for same-event pairs.

Embedding similarity (the Editor) misses duplicates that are worded differently. After
scoring, the leading candidates are shown to a reasoning model, which returns groups of
stories that report the same event. Those groups are merged in code, so the writer never
sees a duplicate. Any failure here means "no merges": it can never block the briefing.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from ..config import DedupeConfig
from ..logging_setup import log
from ..models import StoryCluster
from .cluster import UnionFind, _representative

SYSTEM_PROMPT = """\
You are the duplicate checker for a daily news briefing. You get a numbered list of \
candidate stories. Find stories that report the SAME EVENT, so they can be merged.

Same event (merge): the same announcement, deal, ruling, data release, incident or \
earnings report, covered by different outlets or from different angles (e.g. the news \
itself and a "what it means" piece about that same news).

Not the same event (do not merge): stories that only share a company, person, sector or \
theme; a follow-up development that is news in its own right; two different announcements \
by the same company. When in doubt, do not merge.

Answer with JSON only, no prose and no code fence:
{"duplicate_groups": [["S1", "S4"], ["S2", "S7", "S9"]]}
Each group lists two or more story ids that are one event. Use each id at most once. \
If there are no duplicates answer {"duplicate_groups": []}."""

JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


class DuplicateJudge(Protocol):
    def find_duplicates(self, clusters: list[StoryCluster]) -> list[list[str]]:
        """Returns groups of cluster_ids that are the same event."""
        ...


def story_lines(clusters: list[StoryCluster], summary_chars: int = 300) -> str:
    """The candidate list shown to the model. Ids are S1..Sn in the order given."""
    blocks = []
    for i, c in enumerate(clusters, 1):
        rep = c.representative
        lines = [f"S{i}: {rep.title}", f"  sources: {', '.join(c.sources[:6])}"]
        other_titles = [m.title for m in c.members if m.title != rep.title][:2]
        for title in other_titles:
            lines.append(f"  also reported as: {title}")
        summary = next((m.summary for m in [rep, *c.members] if m.summary), None)
        if summary:
            lines.append(f"  summary: {' '.join(summary.split())[:summary_chars]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def parse_groups(text: str, clusters: list[StoryCluster], max_group_size: int) -> list[list[str]]:
    """Turn the model's answer into validated groups of cluster_ids.

    Raises ValueError if the answer is not the expected JSON. Unknown ids are dropped, and
    a group larger than ``max_group_size`` is discarded as a likely topic-level over-merge.
    """
    match = JSON_RE.search(text)
    if not match:
        raise ValueError("no JSON object in the model answer")
    data = json.loads(match.group(0))
    raw_groups = data.get("duplicate_groups") if isinstance(data, dict) else None
    if not isinstance(raw_groups, list):
        raise ValueError("missing 'duplicate_groups' list")

    by_label = {f"S{i}": c.cluster_id for i, c in enumerate(clusters, 1)}
    groups: list[list[str]] = []
    for raw in raw_groups:
        if not isinstance(raw, list):
            continue
        ids = list(dict.fromkeys(by_label[x] for x in raw if isinstance(x, str) and x in by_label))
        if len(ids) < 2:
            continue
        if len(ids) > max_group_size:
            log.warning("llm_dedupe_group_skipped", size=len(ids), limit=max_group_size)
            continue
        groups.append(ids)
    return groups


def merge_duplicates(
    ranked: list[StoryCluster], groups: list[list[str]]
) -> tuple[list[StoryCluster], dict[str, list[StoryCluster]]]:
    """Fold each duplicate group into its best-ranked story.

    Returns the remaining stories (same order as ``ranked``) and, per surviving cluster_id,
    the stories it absorbed. The survivor keeps its id, status and history and gains the
    other stories' articles. Overlapping groups are combined.
    """
    index = {c.cluster_id: i for i, c in enumerate(ranked)}
    uf = UnionFind(len(ranked))
    for group in groups:
        known = [index[cid] for cid in group if cid in index]
        for other in known[1:]:
            uf.union(known[0], other)

    absorbed: dict[str, list[StoryCluster]] = {}
    for i, cluster in enumerate(ranked):
        root = uf.find(i)  # lowest index in the component = best-ranked story
        if root != i:
            absorbed.setdefault(ranked[root].cluster_id, []).append(cluster)
    if not absorbed:
        return ranked, {}

    gone = {c.cluster_id for dupes in absorbed.values() for c in dupes}
    for keeper in ranked:
        dupes = absorbed.get(keeper.cluster_id)
        if not dupes:
            continue
        members = {m.id: m for m in keeper.members}
        for dupe in dupes:
            for m in dupe.members:
                members.setdefault(m.id, m)
        merged = list(members.values())
        keeper.members = sorted(merged, key=lambda a: -a.authority_weight)
        keeper.representative = _representative(merged)
        keeper.first_seen = min([keeper.first_seen, *(d.first_seen for d in dupes)])
    return [c for c in ranked if c.cluster_id not in gone], absorbed


class BedrockJudge:
    """One Bedrock Converse call per run, with extended thinking where the model supports it."""

    def __init__(self, cfg: DedupeConfig, region: str | None = None):
        import boto3
        from botocore.config import Config

        self.cfg = cfg
        self.model_id = cfg.model.removeprefix("bedrock/")
        self.client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(read_timeout=180, retries={"max_attempts": 3, "mode": "standard"}),
        )

    def _converse(self, prompt: str, thinking: bool) -> str:
        budget = self.cfg.thinking_budget_tokens
        kwargs: dict = {
            "modelId": self.model_id,
            "system": [{"text": SYSTEM_PROMPT}],
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
        }
        if thinking:
            # Extended thinking: no temperature allowed, and maxTokens must exceed the budget.
            kwargs["inferenceConfig"] = {"maxTokens": budget + self.cfg.max_output_tokens}
            kwargs["additionalModelRequestFields"] = {
                "thinking": {"type": "enabled", "budget_tokens": budget}
            }
        else:
            kwargs["inferenceConfig"] = {"maxTokens": self.cfg.max_output_tokens, "temperature": 0}
        resp = self.client.converse(**kwargs)
        blocks = resp["output"]["message"]["content"]
        return "".join(b["text"] for b in blocks if "text" in b)  # skips reasoning blocks

    def find_duplicates(self, clusters: list[StoryCluster]) -> list[list[str]]:
        from botocore.exceptions import ClientError

        prompt = f"Candidate stories:\n\n{story_lines(clusters)}"
        thinking = self.cfg.thinking_budget_tokens > 0
        try:
            text = self._converse(prompt, thinking)
        except ClientError as exc:
            # A model without extended thinking rejects the request: retry as a plain call.
            if not thinking or exc.response["Error"]["Code"] != "ValidationException":
                raise
            log.warning("llm_dedupe_thinking_unsupported", model=self.model_id,
                        error=str(exc)[:200])
            text = self._converse(prompt, thinking=False)
        return parse_groups(text, clusters, self.cfg.max_group_size)


def build_judge(cfg: DedupeConfig, region: str | None = None) -> DuplicateJudge | None:
    return BedrockJudge(cfg, region) if cfg.enabled else None
