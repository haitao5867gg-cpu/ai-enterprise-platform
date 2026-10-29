from typing import Any

import pytest
from aiomysql import Connection
from conftest import query
from mcp import Client

from aep_mysql.config import quote_identifier
from aep_mysql.database import ConnectorError, Database


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5000])
async def test_pages_match_sample_encoding_without_gaps_or_duplicates(
    database: Database, odd_tables: tuple[str, str], limit: int
) -> None:
    schema, table = odd_tables
    expected = (await database.sample_rows(schema, table, 100))["rows"]
    columns = [c["name"] for c in (await database.describe_table(schema, table))["columns"]]
    rows = []
    offset = 0
    while True:
        page = await database.read_rows(schema, table, offset, limit)
        assert page["schema"] == schema and page["table"] == table
        assert page["columns"] == columns
        rows.extend(page["rows"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + limit
        offset = page["next_offset"]
        assert offset < len(expected)
    assert rows == expected
    assert len({row[columns[0]] for row in rows}) == len(expected)
    assert await database.read_rows(schema, table, len(expected), limit) == {
        "schema": schema,
        "table": table,
        "columns": columns,
        "rows": [],
        "next_offset": None,
    }
    assert (await database.read_rows(schema, "empty"))["next_offset"] is None
    assert (await database.read_rows(schema, "empty"))["rows"] == []


@pytest.mark.parametrize("primary_key", [True, False])
async def test_composite_key_order_or_all_columns_including_invisible_columns(
    database: Database, admin: Connection, odd_tables: tuple[str, str], primary_key: bool
) -> None:
    schema = odd_tables[0]
    table = f"{quote_identifier(schema)}.{quote_identifier('pages %s`')}"
    key = ", PRIMARY KEY (second, first)" if primary_key else ""
    await query(admin, f"CREATE TABLE {table} (first int, second int INVISIBLE{key})")
    await query(admin, f"INSERT INTO {table} (first, second) VALUES (2, 2), (1, 2), (2, 1), (1, 1)")
    await query(admin, f"GRANT SELECT ON {table} TO 'source_reader'@'%'")
    expected = [(1, 1), (2, 1), (1, 2), (2, 2)] if primary_key else [(1, 1), (1, 2), (2, 1), (2, 2)]
    pages = [await database.read_rows(schema, "pages %s`", i, 1) for i in range(4)]
    assert [p["rows"][0] for p in pages] == [
        dict(zip(["first", "second"], row, strict=True)) for row in expected
    ]
    assert [p["next_offset"] for p in pages] == [1, 2, 3, None]
    assert all(p["columns"] == ["first", "second"] for p in pages)


@pytest.mark.parametrize("limit", [0, -1, 5001, 1000000, True, 1.5, "10", "1; DELETE FROM x"])
async def test_limit_is_strict(database: Database, limit: Any) -> None:
    with pytest.raises(ConnectorError, match="limit"):
        await database.read_rows("sales", "orders", limit=limit)


@pytest.mark.parametrize("offset", [-1, True, 1.5, "0", "0; DELETE FROM x"])
async def test_offset_is_strict(database: Database, offset: Any) -> None:
    with pytest.raises(ConnectorError, match="offset"):
        await database.read_rows("sales", "orders", offset=offset)


@pytest.mark.parametrize("table", ["hidden", "a_view", "column_only", "missing"])
async def test_unreadable_tables_are_rejected(
    database: Database, odd_tables: tuple[str, str], table: str
) -> None:
    with pytest.raises(ConnectorError, match="does not exist or SELECT"):
        await database.read_rows(odd_tables[0], table)


@pytest.mark.parametrize("name", ["", "a\x00b", "a\nb", "a" * 65, "tail ", "😀", "\ud800"])
async def test_invalid_identifiers_are_rejected(database: Database, name: str) -> None:
    with pytest.raises(ValueError, match="identifiers"):
        await database.read_rows("sales", name)
    with pytest.raises(ValueError, match="identifiers"):
        await database.read_rows(name, "orders")


async def test_mcp_read_only_annotation_pagination_and_validation(
    mcp_url: str, odd_tables: tuple[str, str]
) -> None:
    schema, table = odd_tables
    async with Client(mcp_url) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "read_rows")
        assert tool.annotations and tool.annotations.read_only_hint
        for args in [
            {"limit": 0},
            {"limit": 5001},
            {"limit": True},
            {"limit": "1"},
            {"limit": 1.5},
            {"offset": -1},
            {"offset": True},
            {"offset": "0"},
            {"offset": 1.5},
            {"table": "hidden"},
            {"table": "bad\x00name"},
        ]:
            result = await client.call_tool("read_rows", {"schema": schema, "table": table, **args})
            assert result.is_error
        result = await client.call_tool("read_rows", {"schema": schema, "table": table})
        assert not result.is_error
        assert result.structured_content["next_offset"] is None
        assert len(result.structured_content["rows"]) == 3
        result = await client.call_tool(
            "read_rows", {"schema": schema, "table": table, "offset": 1, "limit": 1}
        )
        assert not result.is_error
        page = result.structured_content
        assert page["next_offset"] == 2 and page["rows"][0][page["columns"][0]] == 2
