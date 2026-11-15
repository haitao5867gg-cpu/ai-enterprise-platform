"""Media libraries and their items, with interchangeable in-memory and Postgres stores.

A library holds the records; the files live under ``AEP_MEDIA_DIR`` at each item's
``rel_path`` (``<library_id>/<item_id>/<name>``), written and removed by aep.media.files.
"""

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from psycopg import errors
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aep.runtime.store import DEFAULT_TENANT

MediaKind = Literal["video", "audio", "image"]
ItemStatus = Literal["processing", "ready", "error"]
STATUSES: tuple[ItemStatus, ...] = ("processing", "ready", "error")
SourceKind = Literal["upload", "stock", "run"]


def _now() -> datetime:
    return datetime.now(UTC)


class DuplicateName(Exception):
    pass


class MissingLibrary(Exception):
    pass


class ItemLimit(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"一个媒体库最多 {limit} 个素材")
        self.limit = limit


@dataclass(frozen=True)
class MediaLibrary:
    library_id: str
    name: str
    description: str = ""
    tenant_id: str = DEFAULT_TENANT
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    item_count: int = 0
    total_bytes: int = 0
    status_counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STATUSES, 0))


@dataclass(frozen=True)
class MediaItem:
    item_id: str
    library_id: str
    name: str
    kind: MediaKind
    size_bytes: int
    rel_path: str
    status: ItemStatus = "processing"
    error: str | None = None
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    description: str = ""
    tags: tuple[str, ...] = ()
    usable: bool = True
    source: dict[str, Any] = field(default_factory=lambda: {"kind": "upload"})
    uploaded_by: str | None = None
    tenant_id: str = DEFAULT_TENANT
    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)


@dataclass(frozen=True)
class ItemQuery:
    """Which items a listing returns: every word of ``q`` in the name, description or
    tags (case-insensitive), and the other filters when given."""

    q: str = ""
    tag: str | None = None
    kind: MediaKind | None = None
    usable: bool | None = None
    limit: int = 50
    offset: int = 0

    @property
    def words(self) -> list[str]:
        return [w.casefold() for w in self.q.split()]


def _matches(item: MediaItem, query: ItemQuery) -> bool:
    if query.tag is not None and query.tag not in item.tags:
        return False
    if query.kind is not None and item.kind != query.kind:
        return False
    if query.usable is not None and item.usable != query.usable:
        return False
    text = " ".join((item.name, item.description, *item.tags)).casefold()
    return all(word in text for word in query.words)


def _newest_first(item: MediaItem) -> tuple[float, str]:
    return (-item.created_at.timestamp(), item.name)


ITEM_FIELDS = ("description", "tags", "usable", "source")
PROBE_FIELDS = ("status", "error", "duration_seconds", "width", "height")


class MediaStore(Protocol):
    async def setup(self) -> None: ...
    async def create_library(self, name: str, description: str = "") -> MediaLibrary: ...
    async def libraries(self) -> list[MediaLibrary]: ...
    async def library(self, library_id: str) -> MediaLibrary | None: ...
    async def update_library(self, library_id: str, **changes: Any) -> MediaLibrary | None: ...
    async def delete_library(self, library_id: str) -> bool: ...
    async def add_items(
        self, library_id: str, items: list[MediaItem], max_items: int
    ) -> list[MediaItem]: ...
    async def items(self, library_id: str, query: ItemQuery) -> tuple[list[MediaItem], int]: ...
    async def item(self, library_id: str, item_id: str) -> MediaItem | None: ...
    async def update_item(
        self, library_id: str, item_id: str, **changes: Any
    ) -> MediaItem | None: ...
    async def delete_item(self, library_id: str, item_id: str) -> MediaItem | None: ...


class MemoryMediaStore:
    """In-process store for tests."""

    def __init__(self) -> None:
        self._libraries: dict[str, MediaLibrary] = {}
        self._items: dict[str, MediaItem] = {}

    async def setup(self) -> None:
        return None

    def _with_counts(self, library: MediaLibrary) -> MediaLibrary:
        items = [i for i in self._items.values() if i.library_id == library.library_id]
        counts = dict.fromkeys(STATUSES, 0)
        for item in items:
            counts[item.status] += 1
        return dataclasses.replace(
            library,
            item_count=len(items),
            total_bytes=sum(i.size_bytes for i in items),
            status_counts=counts,
        )

    async def create_library(self, name: str, description: str = "") -> MediaLibrary:
        if any(lib.name == name for lib in self._libraries.values()):
            raise DuplicateName(name)
        library = MediaLibrary(str(uuid.uuid4()), name, description)
        self._libraries[library.library_id] = library
        return library

    async def libraries(self) -> list[MediaLibrary]:
        return [self._with_counts(lib) for lib in sorted(self._libraries.values(), key=_by_name)]

    async def library(self, library_id: str) -> MediaLibrary | None:
        library = self._libraries.get(library_id)
        return self._with_counts(library) if library else None

    async def update_library(self, library_id: str, **changes: Any) -> MediaLibrary | None:
        library = self._libraries.get(library_id)
        if library is None:
            return None
        name = changes.get("name")
        if name is not None and any(
            lib.name == name and lib.library_id != library_id for lib in self._libraries.values()
        ):
            raise DuplicateName(name)
        values = {
            k: v for k, v in changes.items() if k in ("name", "description") and v is not None
        }
        library = dataclasses.replace(library, **values, updated_at=_now())
        self._libraries[library_id] = library
        return self._with_counts(library)

    async def delete_library(self, library_id: str) -> bool:
        if self._libraries.pop(library_id, None) is None:
            return False
        for item_id in [i.item_id for i in self._items.values() if i.library_id == library_id]:
            del self._items[item_id]
        return True

    async def add_items(
        self, library_id: str, items: list[MediaItem], max_items: int
    ) -> list[MediaItem]:
        if library_id not in self._libraries:
            raise MissingLibrary(library_id)
        count = sum(i.library_id == library_id for i in self._items.values())
        if count + len(items) > max_items:
            raise ItemLimit(max_items)
        now = _now()
        added = [
            dataclasses.replace(item, library_id=library_id, created_at=now, updated_at=now)
            for item in items
        ]
        for item in added:
            self._items[item.item_id] = item
        self._libraries[library_id] = dataclasses.replace(
            self._libraries[library_id], updated_at=now
        )
        return added

    async def items(self, library_id: str, query: ItemQuery) -> tuple[list[MediaItem], int]:
        found = sorted(
            (i for i in self._items.values() if i.library_id == library_id and _matches(i, query)),
            key=_newest_first,
        )
        return found[query.offset : query.offset + query.limit], len(found)

    async def item(self, library_id: str, item_id: str) -> MediaItem | None:
        item = self._items.get(item_id)
        return item if item is not None and item.library_id == library_id else None

    async def update_item(self, library_id: str, item_id: str, **changes: Any) -> MediaItem | None:
        item = await self.item(library_id, item_id)
        if item is None:
            return None
        values = {k: v for k, v in changes.items() if k in ITEM_FIELDS + PROBE_FIELDS}
        if "tags" in values:
            values["tags"] = tuple(values["tags"])
        item = dataclasses.replace(item, **values, updated_at=_now())
        self._items[item_id] = item
        return item

    async def delete_item(self, library_id: str, item_id: str) -> MediaItem | None:
        item = await self.item(library_id, item_id)
        if item is not None:
            del self._items[item_id]
        return item


def _by_name(library: MediaLibrary) -> str:
    return library.name


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS media_libraries (
    library_id  uuid PRIMARY KEY,
    tenant_id   text NOT NULL DEFAULT 'default',
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
)""",
    """
CREATE TABLE IF NOT EXISTS media_items (
    item_id          uuid PRIMARY KEY,
    tenant_id        text NOT NULL DEFAULT 'default',
    library_id       uuid NOT NULL REFERENCES media_libraries ON DELETE CASCADE,
    name             text NOT NULL,
    kind             text NOT NULL,
    size_bytes       bigint NOT NULL,
    rel_path         text NOT NULL,
    status           text NOT NULL,
    error            text,
    duration_seconds double precision,
    width            integer,
    height           integer,
    description      text NOT NULL DEFAULT '',
    tags             text[] NOT NULL DEFAULT '{}',
    usable           boolean NOT NULL DEFAULT true,
    source           jsonb NOT NULL DEFAULT '{"kind": "upload"}',
    uploaded_by      text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
)""",
    "CREATE INDEX IF NOT EXISTS media_items_library ON media_items (library_id, created_at DESC)",
]

LIBRARY_COLUMNS = (
    "l.library_id::text, l.name, l.description, l.tenant_id, l.created_at, l.updated_at"
)
LIBRARY_COUNTS = """
  coalesce(count(i.item_id), 0)::int AS item_count,
  coalesce(sum(i.size_bytes), 0)::bigint AS total_bytes,
  count(i.item_id) FILTER (WHERE i.status = 'processing')::int AS processing,
  count(i.item_id) FILTER (WHERE i.status = 'ready')::int AS ready,
  count(i.item_id) FILTER (WHERE i.status = 'error')::int AS error"""
ITEM_COLUMNS = (
    "item_id::text, library_id::text, name, kind, size_bytes, rel_path, status, error,"
    " duration_seconds, width, height, description, tags, usable, source, uploaded_by,"
    " tenant_id, created_at, updated_at"
)


def _library(row: dict[str, Any]) -> MediaLibrary:
    counts = {status: row.pop(status) for status in STATUSES}
    return MediaLibrary(**row, status_counts=counts)


def _item(row: dict[str, Any]) -> MediaItem:
    return MediaItem(**{**row, "tags": tuple(row["tags"])})


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _like(word: str) -> str:
    escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class PostgresMediaStore:
    """Media records in Postgres. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def _libraries(self, where: str = "", params: tuple[Any, ...] = ()) -> list[MediaLibrary]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {LIBRARY_COLUMNS}, {LIBRARY_COUNTS}"
                " FROM media_libraries l LEFT JOIN media_items i ON i.library_id = l.library_id"
                f" WHERE l.tenant_id = %s {where} GROUP BY l.library_id ORDER BY l.name",
                (DEFAULT_TENANT, *params),
            )
            return [_library(row) for row in await cur.fetchall()]

    async def create_library(self, name: str, description: str = "") -> MediaLibrary:
        key = uuid.uuid4()
        async with self._pool.connection() as conn:
            try:
                await conn.execute(
                    "INSERT INTO media_libraries (library_id, name, description)"
                    " VALUES (%s, %s, %s)",
                    (key, name, description),
                )
            except errors.UniqueViolation as exc:
                raise DuplicateName(name) from exc
        library = await self.library(str(key))
        assert library is not None
        return library

    async def libraries(self) -> list[MediaLibrary]:
        return await self._libraries()

    async def library(self, library_id: str) -> MediaLibrary | None:
        key = _uuid(library_id)
        if key is None:
            return None
        found = await self._libraries("AND l.library_id = %s", (key,))
        return found[0] if found else None

    async def update_library(self, library_id: str, **changes: Any) -> MediaLibrary | None:
        key = _uuid(library_id)
        if key is None:
            return None
        async with self._pool.connection() as conn:
            try:
                cur = await conn.execute(
                    "UPDATE media_libraries SET name = coalesce(%s, name),"
                    " description = coalesce(%s, description), updated_at = now()"
                    " WHERE library_id = %s AND tenant_id = %s RETURNING library_id",
                    (changes.get("name"), changes.get("description"), key, DEFAULT_TENANT),
                )
            except errors.UniqueViolation as exc:
                raise DuplicateName(changes.get("name")) from exc
            if await cur.fetchone() is None:
                return None
        return await self.library(library_id)

    async def delete_library(self, library_id: str) -> bool:
        key = _uuid(library_id)
        if key is None:
            return False
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM media_libraries WHERE library_id = %s AND tenant_id = %s"
                " RETURNING library_id",
                (key, DEFAULT_TENANT),
            )
            return await cur.fetchone() is not None

    async def add_items(
        self, library_id: str, items: list[MediaItem], max_items: int
    ) -> list[MediaItem]:
        key = _uuid(library_id)
        if key is None:
            raise MissingLibrary(library_id)
        async with self._pool.connection() as conn, conn.transaction():
            # Lock the library so two uploads cannot both pass the limit.
            cur = await conn.execute(
                "SELECT library_id FROM media_libraries WHERE library_id = %s AND tenant_id = %s"
                " FOR UPDATE",
                (key, DEFAULT_TENANT),
            )
            if await cur.fetchone() is None:
                raise MissingLibrary(library_id)
            cur = await conn.execute(
                "SELECT count(*)::int AS n FROM media_items WHERE library_id = %s", (key,)
            )
            row = await cur.fetchone()
            if row["n"] + len(items) > max_items:
                raise ItemLimit(max_items)
            added: list[MediaItem] = []
            for item in items:
                cur = await conn.execute(
                    "INSERT INTO media_items (item_id, library_id, name, kind, size_bytes,"
                    " rel_path, status, error, duration_seconds, width, height, description,"
                    " tags, usable, source, uploaded_by)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    f" RETURNING {ITEM_COLUMNS}",
                    (
                        uuid.UUID(item.item_id),
                        key,
                        item.name,
                        item.kind,
                        item.size_bytes,
                        item.rel_path,
                        item.status,
                        item.error,
                        item.duration_seconds,
                        item.width,
                        item.height,
                        item.description,
                        list(item.tags),
                        item.usable,
                        Jsonb(item.source),
                        item.uploaded_by,
                    ),
                )
                added.append(_item(await cur.fetchone()))
            await conn.execute(
                "UPDATE media_libraries SET updated_at = now() WHERE library_id = %s", (key,)
            )
            return added

    async def items(self, library_id: str, query: ItemQuery) -> tuple[list[MediaItem], int]:
        key = _uuid(library_id)
        if key is None:
            return [], 0
        where = ["library_id = %s", "tenant_id = %s"]
        params: list[Any] = [key, DEFAULT_TENANT]
        for word in query.words:
            where.append(
                "lower(name || ' ' || description || ' ' || array_to_string(tags, ' '))"
                " LIKE %s ESCAPE '\\'"
            )
            params.append(_like(word))
        if query.tag is not None:
            where.append("%s = ANY(tags)")
            params.append(query.tag)
        if query.kind is not None:
            where.append("kind = %s")
            params.append(query.kind)
        if query.usable is not None:
            where.append("usable = %s")
            params.append(query.usable)
        condition = " AND ".join(where)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT count(*)::int AS n FROM media_items WHERE {condition}", tuple(params)
            )
            total = (await cur.fetchone())["n"]
            cur = await conn.execute(
                f"SELECT {ITEM_COLUMNS} FROM media_items WHERE {condition}"
                " ORDER BY created_at DESC, name LIMIT %s OFFSET %s",
                (*params, query.limit, query.offset),
            )
            return [_item(row) for row in await cur.fetchall()], total

    async def item(self, library_id: str, item_id: str) -> MediaItem | None:
        library, key = _uuid(library_id), _uuid(item_id)
        if library is None or key is None:
            return None
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {ITEM_COLUMNS} FROM media_items"
                " WHERE item_id = %s AND library_id = %s AND tenant_id = %s",
                (key, library, DEFAULT_TENANT),
            )
            row = await cur.fetchone()
            return _item(row) if row else None

    async def update_item(self, library_id: str, item_id: str, **changes: Any) -> MediaItem | None:
        library, key = _uuid(library_id), _uuid(item_id)
        if library is None or key is None:
            return None
        values = {k: v for k, v in changes.items() if k in ITEM_FIELDS + PROBE_FIELDS}
        if not values:
            return await self.item(library_id, item_id)
        sets, params = [], []
        for name, value in values.items():
            sets.append(f"{name} = %s")
            if name == "tags":
                value = list(value)
            elif name == "source":
                value = Jsonb(value)
            params.append(value)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE media_items SET {', '.join(sets)}, updated_at = now()"
                " WHERE item_id = %s AND library_id = %s AND tenant_id = %s"
                f" RETURNING {ITEM_COLUMNS}",
                (*params, key, library, DEFAULT_TENANT),
            )
            row = await cur.fetchone()
            return _item(row) if row else None

    async def delete_item(self, library_id: str, item_id: str) -> MediaItem | None:
        library, key = _uuid(library_id), _uuid(item_id)
        if library is None or key is None:
            return None
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"DELETE FROM media_items WHERE item_id = %s AND library_id = %s"
                f" AND tenant_id = %s RETURNING {ITEM_COLUMNS}",
                (key, library, DEFAULT_TENANT),
            )
            row = await cur.fetchone()
            return _item(row) if row else None
