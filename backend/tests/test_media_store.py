"""Media stores: the in-memory one always, the Postgres one when AEP_TEST_DATABASE_URL is set
(CI sets it). Both must answer the same way."""

import os
import uuid

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.media.store import (
    DuplicateId,
    DuplicateName,
    ItemLimit,
    ItemQuery,
    MediaItem,
    MemoryMediaStore,
    MissingLibrary,
    PostgresMediaStore,
)

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")


@pytest.fixture(params=["memory", "postgres"])
async def store(request):
    if request.param == "memory":
        yield MemoryMediaStore()
        return
    if not DATABASE_URL:
        pytest.skip("AEP_TEST_DATABASE_URL not set")
    pool = AsyncConnectionPool(
        DATABASE_URL, open=False, kwargs={"autocommit": True, "row_factory": dict_row}
    )
    await pool.open(wait=True)
    store = PostgresMediaStore(pool)
    await store.setup()
    yield store
    await pool.close()


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _item(library_id: str, name: str, **values) -> MediaItem:
    item_id = str(uuid.uuid4())
    defaults = {
        "kind": "video",
        "size_bytes": 100,
        "status": "ready",
    }
    return MediaItem(
        item_id=item_id,
        library_id=library_id,
        name=name,
        rel_path=f"{library_id}/{item_id}/{name}",
        **{**defaults, **values},
    )


async def test_library_crud(store):
    name = _name("教练素材")
    library = await store.create_library(name, "王教练自己拍的")
    assert (library.name, library.description, library.item_count) == (name, "王教练自己拍的", 0)
    assert library.status_counts == {"processing": 0, "ready": 0, "error": 0}

    assert (await store.library(library.library_id)).name == name
    assert name in [lib.name for lib in await store.libraries()]
    assert await store.library("not-a-uuid") is None
    assert await store.library(str(uuid.uuid4())) is None
    with pytest.raises(DuplicateName):
        await store.create_library(name)

    other = await store.create_library(_name("网络素材"))
    with pytest.raises(DuplicateName):
        await store.update_library(other.library_id, name=name)
    renamed = await store.update_library(library.library_id, name=f"{name}-2")
    assert (renamed.name, renamed.description) == (f"{name}-2", "王教练自己拍的")
    described = await store.update_library(library.library_id, description="")
    assert (described.name, described.description) == (f"{name}-2", "")
    assert await store.update_library(str(uuid.uuid4()), name="x") is None

    assert await store.delete_library(library.library_id) is True
    assert await store.delete_library(library.library_id) is False
    assert await store.library(library.library_id) is None
    assert await store.delete_library("bad") is False


async def test_items_counts_limit_and_cascade(store):
    library = await store.create_library(_name("教练素材"))
    lid = library.library_id
    added = await store.add_items(
        lid,
        [
            _item(lid, "卧推.mp4", size_bytes=300, tags=("胸", "卧推"), uploaded_by="王教练"),
            _item(lid, "配音.m4a", kind="audio", size_bytes=50, status="processing"),
        ],
        max_items=3,
    )
    assert [i.name for i in added] == ["卧推.mp4", "配音.m4a"]
    assert added[0].tags == ("胸", "卧推") and added[0].uploaded_by == "王教练"
    assert added[0].source == {"kind": "upload"} and added[0].usable is True

    counted = await store.library(lid)
    assert (counted.item_count, counted.total_bytes) == (2, 350)
    assert counted.status_counts == {"processing": 1, "ready": 1, "error": 0}

    with pytest.raises(ItemLimit):
        await store.add_items(lid, [_item(lid, "a.mp4"), _item(lid, "b.mp4")], max_items=3)
    assert (await store.library(lid)).item_count == 2
    with pytest.raises(MissingLibrary):
        await store.add_items(str(uuid.uuid4()), [_item(lid, "c.mp4")], max_items=3)
    with pytest.raises(MissingLibrary):
        await store.add_items("bad", [_item(lid, "c.mp4")], max_items=3)

    first = added[0]
    assert await store.item(lid, first.item_id) == first
    other = await store.create_library(_name("网络素材"))
    assert await store.item(other.library_id, first.item_id) is None
    assert await store.item(lid, "bad") is None

    await store.delete_library(lid)
    assert await store.item(lid, first.item_id) is None


async def test_search_filters_and_pages(store):
    library = await store.create_library(_name("教练素材"))
    lid = library.library_id
    await store.add_items(
        lid,
        [
            _item(lid, "Bench-侧面.mp4", description="杠铃卧推，侧面全景", tags=("胸",)),
            _item(lid, "划船.mov", description="哑铃划船 100%力量", tags=("背",)),
            _item(lid, "封面.jpg", kind="image", tags=("封面", "胸")),
            _item(lid, "old_clip.mp4", usable=False),
        ],
        max_items=100,
    )
    later = await store.add_items(lid, [_item(lid, "开头.mp4", tags=("开头",))], max_items=100)

    async def names(**query) -> tuple[list[str], int]:
        items, total = await store.items(lid, ItemQuery(**query))
        return [i.name for i in items], total

    everything, total = await names()
    # Newest first; one upload's items by name (the database's collation).
    assert total == 5 and everything[0] == later[0].name
    assert await names(q="bench 卧推") == (["Bench-侧面.mp4"], 1)
    assert await names(q="BENCH 划船") == ([], 0)
    chest, total = await names(q="胸")
    assert (sorted(chest), total) == (["Bench-侧面.mp4", "封面.jpg"], 2)
    # LIKE wildcards are matched as they are.
    assert await names(q="100%") == (["划船.mov"], 1)
    assert await names(q="_") == (["old_clip.mp4"], 1)
    chest, total = await names(tag="胸")
    assert (sorted(chest), total) == (["Bench-侧面.mp4", "封面.jpg"], 2)
    assert await names(tag="胸部") == ([], 0)
    assert await names(kind="image") == (["封面.jpg"], 1)
    assert (await names(usable=False))[1] == 1
    assert (await names(usable=True))[1] == 4
    assert await names(limit=2, offset=1) == (everything[1:3], 5)
    assert await store.items(str(uuid.uuid4()), ItemQuery()) == ([], 0)
    assert await store.items("bad", ItemQuery()) == ([], 0)


async def test_update_and_delete_items(store):
    library = await store.create_library(_name("教练素材"))
    lid = library.library_id
    (item,) = await store.add_items(lid, [_item(lid, "卧推.mp4")], max_items=10)

    source = {
        "kind": "stock",
        "site": "Pexels",
        "url": "https://www.pexels.com/video/1/",
        "author": "someone",
        "license": "Pexels License",
        "license_url": "https://www.pexels.com/license/",
        "run_id": None,
    }
    changed = await store.update_item(
        lid, item.item_id, description="侧面", tags=["胸", "卧推"], usable=False, source=source
    )
    assert changed.description == "侧面" and changed.tags == ("胸", "卧推")
    assert changed.usable is False and changed.source == source
    assert changed.updated_at >= item.updated_at

    probed = await store.update_item(
        lid, item.item_id, status="error", error="不是视频", duration_seconds=None
    )
    assert (probed.status, probed.error, probed.tags) == ("error", "不是视频", ("胸", "卧推"))
    probed = await store.update_item(
        lid, item.item_id, status="ready", error=None, duration_seconds=12.5, width=720, height=1280
    )
    assert (probed.duration_seconds, probed.width, probed.height) == (12.5, 720, 1280)
    assert (await store.library(lid)).status_counts["ready"] == 1
    # Fields a client may not set are ignored.
    same = await store.update_item(lid, item.item_id, name="x.mp4", rel_path="../x")
    assert (same.name, same.rel_path) == (item.name, item.rel_path)

    assert await store.update_item(lid, str(uuid.uuid4()), usable=True) is None
    assert await store.update_item("bad", item.item_id, usable=True) is None

    deleted = await store.delete_item(lid, item.item_id)
    assert deleted.item_id == item.item_id
    assert await store.delete_item(lid, item.item_id) is None
    assert (await store.library(lid)).item_count == 0


async def test_libraries_with_a_given_id_and_the_items_a_run_made(store):
    wanted = str(uuid.uuid4())
    library = await store.create_library(_name("配音"), library_id=wanted)
    assert library.library_id == wanted
    with pytest.raises(DuplicateId):
        await store.create_library(_name("别的"), library_id=wanted)
    with pytest.raises(DuplicateName):
        await store.create_library(library.name, library_id=str(uuid.uuid4()))

    other = await store.create_library(_name("成片"))
    run = f"run-{uuid.uuid4().hex[:8]}"
    made = [
        _item(wanted, "第一段.mp4", source={"kind": "run", "run_id": run}),
        _item(wanted, "别的运行.mp4", source={"kind": "run", "run_id": "someone-else"}),
        _item(wanted, "上传的.mp4", source={"kind": "upload", "run_id": run}),
    ]
    await store.add_items(wanted, made, 10)
    await store.add_items(
        other.library_id,
        [_item(other.library_id, "第二段.mp4", source={"kind": "run", "run_id": run})],
        10,
    )
    found = await store.run_items(run, [wanted, other.library_id], 10)
    assert [i.name for i in found] == ["第一段.mp4", "第二段.mp4"]
    assert [i.name for i in await store.run_items(run, [other.library_id], 10)] == ["第二段.mp4"]
    assert len(await store.run_items(run, [wanted, other.library_id], 1)) == 1
    assert await store.run_items(run, ["not-a-uuid"], 10) == []
    await store.delete_library(wanted)
    await store.delete_library(other.library_id)


async def test_items_waiting_for_a_probe_and_files_a_run_saved(store):
    library = await store.create_library(_name("成片"))
    lid = library.library_id
    waiting = [_item(lid, f"{n}.mp4", status="processing") for n in ("一", "二")]
    saved = _item(lid, "成片.mp4", import_key="k" * 64, source={"kind": "run", "run_id": "r"})
    await store.add_items(lid, [*waiting, _item(lid, "好了.mp4"), saved], 10)
    mine = [i for i in await store.processing(1000) if i.library_id == lid]
    assert [i.name for i in mine] == ["一.mp4", "二.mp4"]
    assert len(await store.processing(1)) == 1

    probed = await store.update_item(
        lid, waiting[0].item_id, status="ready", duration_seconds=3.5, has_thumbnail=True
    )
    assert probed is not None and probed.has_thumbnail and probed.duration_seconds == 3.5
    assert await store.item(lid, waiting[0].item_id) == probed
    assert [i.name for i in await store.processing(1000) if i.library_id == lid] == ["二.mp4"]

    found = await store.imported(lid, "k" * 64)
    assert found is not None and found.item_id == saved.item_id
    assert found.import_key == "k" * 64
    assert await store.imported(lid, "x" * 64) is None
    assert await store.imported("not-a-uuid", "k" * 64) is None
    await store.delete_library(lid)
