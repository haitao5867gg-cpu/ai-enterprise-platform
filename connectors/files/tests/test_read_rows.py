import json
from collections.abc import Callable
from typing import Any

import pytest
from mcp import Client

from aep_files.config import Settings
from aep_files.database import ConnectorError, Database

DatasetFactory = Callable[..., tuple[str, str, dict[str, Any]]]


@pytest.mark.parametrize("limit", [1, 2, 3, 4, 5000])
def test_pages_match_sample_encoding_without_gaps_or_duplicates(
    database: Database, dataset: DatasetFactory, limit: int
) -> None:
    dataset_id, schema, manifest = dataset()
    expected = database.sample_rows(dataset_id, schema, "orders", 100)["rows"]
    columns = [c["name"] for c in manifest["tables"][0]["columns"]]
    rows = []
    offset = 0
    while True:
        page = database.read_rows(dataset_id, schema, "orders", offset, limit)
        assert page["schema"] == schema and page["table"] == "orders"
        assert page["columns"] == columns
        rows.extend(page["rows"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + limit
        offset = page["next_offset"]
        assert offset < len(expected)
    assert rows == expected
    assert len({row["id"] for row in rows}) == len(expected)
    assert database.read_rows(dataset_id, schema, "orders", len(expected), limit) == {
        "schema": schema,
        "table": "orders",
        "columns": columns,
        "rows": [],
        "next_offset": None,
    }


@pytest.mark.parametrize("primary_key", [True, False])
def test_key_or_all_column_order_with_quoted_identifiers(
    database: Database, dataset: DatasetFactory, settings: Settings, primary_key: bool
) -> None:
    table = 'pages %s"'
    dataset_id, schema, manifest = dataset(
        'SELECT * FROM (VALUES (2, 1), (1, 4), (1, 2), (2, 3)) t("group""", id)', table=table
    )
    if primary_key:
        manifest["tables"][0]["columns"][1]["primary_key"] = True
        (settings.uploads_dir / dataset_id / "manifest.json").write_text(json.dumps(manifest))
    expected = [(2, 1), (1, 2), (2, 3), (1, 4)] if primary_key else [(1, 2), (1, 4), (2, 1), (2, 3)]
    pages = [database.read_rows(dataset_id, schema, table, i, 1) for i in range(4)]
    assert [p["rows"][0] for p in pages] == [
        dict(zip(['group"', "id"], row, strict=True)) for row in expected
    ]
    assert [p["next_offset"] for p in pages] == [1, 2, 3, None]


def test_empty_table_and_json_encoding_match_sample(
    database: Database, dataset: DatasetFactory
) -> None:
    dataset_id, schema, _ = dataset("""
        SELECT 1 AS id, 12.50::DECIMAL(12,2) AS amount, DATE '2026-01-01' AS day,
               TIMESTAMP '2026-01-01 12:00:00' AS moment, TIME '12:00:00' AS clock,
               '\\x00\\xFF'::BLOB AS bytes, NULL AS missing
    """)
    page = database.read_rows(dataset_id, schema, "orders")
    assert page["rows"] == database.sample_rows(dataset_id, schema, "orders")["rows"]
    assert page["rows"][0]["amount"] == 12.5
    assert page["rows"][0]["moment"] == "2026-01-01T12:00:00"
    assert page["next_offset"] is None
    dataset_id, schema, _ = dataset("SELECT 1 AS id WHERE false")
    assert database.read_rows(dataset_id, schema, "orders") == {
        "schema": schema,
        "table": "orders",
        "columns": ["id"],
        "rows": [],
        "next_offset": None,
    }


@pytest.mark.parametrize("limit", [0, -1, 5001, 1000000, True, 1.5, "10", "1; DELETE FROM x"])
def test_limit_is_strict(database: Database, dataset: DatasetFactory, limit: Any) -> None:
    dataset_id, schema, _ = dataset()
    with pytest.raises(ConnectorError, match="limit"):
        database.read_rows(dataset_id, schema, "orders", limit=limit)


@pytest.mark.parametrize("offset", [-1, True, 1.5, "0", "0; DELETE FROM x"])
def test_offset_is_strict(database: Database, dataset: DatasetFactory, offset: Any) -> None:
    dataset_id, schema, _ = dataset()
    with pytest.raises(ConnectorError, match="offset"):
        database.read_rows(dataset_id, schema, "orders", offset=offset)


def test_unreadable_tables_and_other_datasets_are_rejected(
    database: Database, dataset: DatasetFactory
) -> None:
    first, schema, _ = dataset()
    _, other_schema, _ = dataset("SELECT 'secret' AS private", table="private")
    for bad_schema, bad_table in [
        (schema, "missing"),
        ("main", "orders"),
        (other_schema, "private"),
    ]:
        with pytest.raises(ConnectorError, match="does not exist or SELECT"):
            database.read_rows(first, bad_schema, bad_table)


@pytest.mark.parametrize("name", ["", "a\x00b", "a\nb", "\ud800"])
def test_invalid_identifiers_are_rejected(
    database: Database, dataset: DatasetFactory, name: str
) -> None:
    dataset_id, schema, _ = dataset()
    with pytest.raises(ConnectorError, match="identifiers"):
        database.read_rows(dataset_id, schema, name)
    with pytest.raises(ConnectorError, match="identifiers"):
        database.read_rows(dataset_id, name, "orders")


async def test_mcp_read_only_annotation_pagination_and_validation(
    mcp_base: str, dataset: DatasetFactory
) -> None:
    dataset_id, schema, _ = dataset()
    async with Client(f"{mcp_base}/datasets/{dataset_id}/mcp") as client:
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
            {"table": "missing"},
            {"table": "bad\x00name"},
        ]:
            result = await client.call_tool(
                "read_rows", {"schema": schema, "table": "orders", **args}
            )
            assert result.is_error
        result = await client.call_tool("read_rows", {"schema": schema, "table": "orders"})
        assert not result.is_error
        assert result.structured_content["next_offset"] is None
        assert len(result.structured_content["rows"]) == 4
        result = await client.call_tool(
            "read_rows", {"schema": schema, "table": "orders", "offset": 1, "limit": 1}
        )
        assert not result.is_error
        page = result.structured_content
        assert page["next_offset"] == 2 and page["rows"][0]["id"] == 2


def test_upload_headers_with_newlines_and_tabs_stay_readable(
    database: Database, dataset: DatasetFactory
) -> None:
    dataset_id, schema, _ = dataset(
        'SELECT * FROM (VALUES (1, 2)) t(id, "金额\n(元)\t含税")', table="lines"
    )
    page = database.read_rows(dataset_id, schema, "lines")
    assert page["rows"] == [{"id": 1, "金额\n(元)\t含税": 2}]
    assert database.sample_rows(dataset_id, schema, "lines")["rows"] == page["rows"]
