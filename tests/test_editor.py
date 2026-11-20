from datetime import timedelta

import numpy as np

from news_aggregator.editor import Editor, StoryMemory, remember
from news_aggregator.editor.cluster import exact_dedupe
from news_aggregator.editor.embed import HashEmbedder
from news_aggregator.models import StoryStatus, utcnow
from news_aggregator.storage import LocalStorage

from .conftest import make_article

FED = "Federal Reserve holds interest rates steady as inflation cools"
CHIP = "Nvidia unveils new data center chip for artificial intelligence workloads"
OIL = "Oil prices jump after OPEC announces surprise production cut"


def _day_one():
    return [
        make_article(FED, "Outlet A", weight=1.3),
        make_article(FED + " - Outlet B", "Outlet B"),
        make_article("Federal Reserve holds interest rates steady, inflation cools", "Outlet C"),
        make_article(CHIP, "Outlet A"),
        make_article(OIL, "Outlet D"),
    ]


def test_exact_dedupe_keeps_highest_authority_copy():
    a = make_article(FED, "Low", url="https://x.com/s", weight=0.5)
    b = make_article(FED, "High", url="https://x.com/s?utm_source=feed", weight=1.4)
    out = exact_dedupe([a, b])
    assert len(out) == 1 and out[0].source == "High"


def test_cluster_groups_duplicates_and_separates_distinct(config):
    editor = Editor(config.tuning.editor, HashEmbedder())
    groups, centroids = editor.cluster(_day_one())
    sizes = sorted(len(g) for g in groups)
    assert sizes == [1, 1, 3]
    assert np.allclose(np.linalg.norm(centroids, axis=1), 1.0, atol=1e-5)


def test_representative_is_highest_authority(config):
    result = Editor(config.tuning.editor, HashEmbedder()).run(_day_one(), StoryMemory(512))
    fed = next(c for c in result.clusters if c.source_count == 3)
    assert fed.representative.source == "Outlet A"
    assert all(c.status == StoryStatus.NEW for c in result.clusters)


def test_cross_day_marks_repeats_and_developing(config, tmp_path):
    storage = LocalStorage(tmp_path)
    embedder = HashEmbedder()
    editor = Editor(config.tuning.editor, embedder)

    memory = StoryMemory(embedder.dim)
    day1 = editor.run(_day_one(), memory)
    remember(memory, day1.clusters, day1)
    memory.save(storage, "run-1")

    # Day 2: Fed story repeats with the same coverage; chip story grows from 1 to 4 sources.
    day2_articles = [
        make_article(FED, "Outlet A", url="https://a.com/fed-day2"),
        make_article(CHIP, "Outlet A", url="https://a.com/chip-day2"),
        make_article(CHIP + " - Outlet E", "Outlet E"),
        make_article(CHIP + " - Outlet F", "Outlet F"),
        make_article(CHIP + " - Outlet G", "Outlet G"),
    ]
    memory2 = StoryMemory.load(storage, embedder.dim)
    day2 = editor.run(day2_articles, memory2)
    status = {c.representative.title.split(" ")[0]: c.status for c in day2.clusters}
    assert status["Federal"] == StoryStatus.REPEAT
    assert status["Nvidia"] == StoryStatus.DEVELOPING
    assert day2.repeats == 1


def test_memory_roundtrip_prune_and_reconcile(config, tmp_path):
    storage = LocalStorage(tmp_path)
    embedder = HashEmbedder()
    memory = StoryMemory(embedder.dim)
    result = Editor(config.tuning.editor, embedder).run(_day_one(), memory)
    for c in result.clusters:
        c.last_seen = utcnow() - timedelta(days=10)
    result.clusters[0].last_seen = utcnow()
    remember(memory, result.clusters, result)
    assert memory.size == 3

    assert memory.prune(utcnow() - timedelta(days=7)) == 2
    assert memory.size == 1 and len(memory.meta) == 1

    memory.save(storage, "run-x")
    loaded = StoryMemory.load(storage, embedder.dim)
    assert loaded.size == 1
    assert list(loaded.meta["run_id"]) == ["run-x"]

    # Simulate metadata/index drift: drop the metadata row, keep the vector.
    loaded.meta = loaded.meta.iloc[0:0]
    loaded._reconcile()
    assert loaded.size == 0


def test_dimension_change_resets_memory(config, tmp_path):
    storage = LocalStorage(tmp_path)
    memory = StoryMemory(512)
    result = Editor(config.tuning.editor, HashEmbedder(512)).run(_day_one(), memory)
    remember(memory, result.clusters, result)
    memory.save(storage, "r")
    assert StoryMemory.load(storage, 384).size == 0
