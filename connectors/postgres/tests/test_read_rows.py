from typing import Any

import pytest
from mcp import Client
from psycopg import AsyncConnection, sql

from aep_postgres.database import ConnectorError, Database


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
async def test_composite_key_order_or_all_columns_with_json(
    database: Database,
    admin: AsyncConnection[dict[str, Any]],
    odd_tables: tuple[str, str],
    primary_key: bool,
) -> None:
    schema = odd_tables[0]
    table = sql.Identifier(schema, 'pages %s"')
    key = ", PRIMARY KEY (second, first)" if primary_key else ""
    await admin.execute(
        sql.SQL("CREATE TABLE {} (first int, second int, payload json" + key + ")").format(table)
    )
    await admin.execute(
        sql.SQL(
            "INSERT INTO {} VALUES (2, 2, 'null'), (1, 2, '{{}}'), (2, 1, '[]'), (1, 1, NULL)"
        ).format(table)
    )
    await admin.execute(sql.SQL("GRANT SELECT ON {} TO source_reader").format(table))
    expected = [(1, 1), (2, 1), (1, 2), (2, 2)] if primary_key else [(1, 1), (1, 2), (2, 1), (2, 2)]
    pages = [await database.read_rows(schema, 'pages %s"', i, 1) for i in range(4)]
    assert [(p["rows"][0]["first"], p["rows"][0]["second"]) for p in pages] == expected
    assert [p["next_offset"] for p in pages] == [1, 2, 3, None]
    assert all(p["columns"] == ["first", "second", "payload"] for p in pages)


@pytest.mark.parametrize("limit", [0, -1, 5001, 1000000, True, 1.5, "10", "1; DELETE FROM x"])
async def test_limit_is_strict(database: Database, limit: Any) -> None:
    with pytest.raises(ConnectorError, match="limit"):
        await database.read_rows("sales", "orders", limit=limit)


@pytest.mark.parametrize("offset", [-1, True, 1.5, "0", "0; DELETE FROM x"])
async def test_offset_is_strict(database: Database, offset: Any) -> None:
    with pytest.raises(ConnectorError, match="offset"):
        await database.read_rows("sales", "orders", offset=offset)


@pytest.mark.parametrize("table", ["hidden", "a_view", "missing"])
async def test_unreadable_tables_are_rejected(
    database: Database, odd_tables: tuple[str, str], table: str
) -> None:
    with pytest.raises(ConnectorError, match="does not exist or SELECT"):
        await database.read_rows(odd_tables[0], table)


@pytest.mark.parametrize("name", ["", "a\x00b", "a\nb", "a" * 64, "中" * 22])
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
