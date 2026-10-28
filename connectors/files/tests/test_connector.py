import asyncio
import json

import duckdb
import httpx2 as httpx
import pytest
from mcp import Client

from aep_files.database import ConnectorError, Database
from aep_files.smoke import check


def test_five_tools_match_postgres_contract(database, dataset):
    dataset_id, schema, manifest = dataset()
    assert database.list_schemas(dataset_id) == {"schemas": [schema]}
    assert database.list_tables(dataset_id, schema) == {"schema": schema, "tables": ["orders"]}
    described = database.describe_table(dataset_id, schema, "orders")
    assert described == {
        "schema": schema,
        "table": "orders",
        "columns": manifest["tables"][0]["columns"],
        "foreign_keys": [],
    }
    assert database.sample_rows(dataset_id, schema, "orders", 1) == {
        "schema": schema,
        "table": "orders",
        "rows": [
            {
                "id": 1,
                "status": "paid",
                "nullable": "x",
                "ordered_at": "2026-10-01",
            }
        ],
    }
    stats = database.table_stats(dataset_id, schema, "orders")
    assert stats["estimated_row_count"] == stats["sample_row_count"] == 4
    assert stats["sample_limit"] == 1000
    status = next(c for c in stats["columns"] if c["name"] == "status")
    assert status == {
        "name": "status",
        "null_fraction": 0.25,
        "distinct_count": 2,
        "top_values": [{"value": "paid", "count": 2}, {"value": "open", "count": 1}],
    }


@pytest.mark.parametrize("limit", [0, 101, True, "10", 1.5])
def test_invalid_limits(database, dataset, limit):
    dataset_id, schema, _ = dataset()
    with pytest.raises(ConnectorError, match="between 1 and 100"):
        database.sample_rows(dataset_id, schema, "orders", limit)


def test_sample_bounds_cardinality_and_empty_tables(settings, dataset):
    dataset_id, schema, _ = dataset("SELECT i AS id FROM range(30) t(i)")
    database = Database(settings.model_copy(update={"files_stats_sample_size": 21}))
    stats = database.table_stats(dataset_id, schema, "orders")
    assert stats["sample_row_count"] == stats["sample_limit"] == 21
    assert stats["estimated_row_count"] == 30
    assert stats["columns"] == [{"name": "id", "distinct_count": 21, "null_fraction": 0.0}]
    dataset_id, schema, _ = dataset("SELECT NULL::VARCHAR AS value WHERE false")
    assert database.sample_rows(dataset_id, schema, "orders")["rows"] == []
    assert database.table_stats(dataset_id, schema, "orders")["columns"] == [
        {"name": "value", "null_fraction": None, "distinct_count": 0}
    ]


def test_top_values_count_before_truncation_and_sort(database, dataset):
    prefix = "x" * 80
    dataset_id, schema, _ = dataset(f"""
        SELECT * FROM (VALUES ('{prefix}a'), ('{prefix}b'), ('{prefix}b'), ('short')) t(value)
    """)
    [column] = database.table_stats(dataset_id, schema, "orders")["columns"]
    assert column["distinct_count"] == 3
    assert column["top_values"] == [
        {"value": prefix, "count": 2, "truncated": True},
        {"value": "short", "count": 1},
        {"value": prefix, "count": 1, "truncated": True},
    ]


def test_quoted_identifiers_and_manifest_allowlist(database, dataset):
    odd = 'Orders "; DROP TABLE x; --'
    dataset_id, schema, _ = dataset('SELECT 42 AS "a""b"', table=odd)
    assert database.sample_rows(dataset_id, schema, odd)["rows"] == [{'a"b': 42}]
    for bad_schema, bad_table in [(schema, "../source.parquet"), ("main", odd), (schema, "orders")]:
        with pytest.raises(ConnectorError, match="does not exist"):
            database.sample_rows(dataset_id, bad_schema, bad_table)


@pytest.mark.parametrize("value", ["nope", "../other", "../../etc/passwd", "/etc/passwd", "a" * 32])
def test_uuid_required(database, value):
    with pytest.raises(ConnectorError, match="UUID"):
        database.list_schemas(value)


def test_datasets_and_files_are_isolated(database, dataset, settings):
    first, schema, _ = dataset()
    second, other_schema, _ = dataset("SELECT 'secret' AS private", table="private")
    assert database.list_tables(first, schema)["tables"] == ["orders"]
    with pytest.raises(ConnectorError):
        database.sample_rows(first, other_schema, "private")
    # Even internal SQL on the configured connection cannot read the other dataset.
    unlisted = settings.uploads_dir / first / "unlisted.parquet"
    unlisted.write_bytes((settings.uploads_dir / second / "source.parquet").read_bytes())
    with database.connect(first) as conn:
        for path in [settings.uploads_dir / second / "source.parquet", unlisted, "/etc/passwd"]:
            with pytest.raises(duckdb.Error, match="[Dd]isabled|[Pp]ermission"):
                conn.execute("SELECT * FROM read_parquet(?)", [str(path)])


@pytest.mark.parametrize("kind", ["parent", "absolute", "symlink", "directory", "manifest"])
def test_manifest_and_symlink_escapes_are_refused(database, dataset, settings, kind):
    first, _, manifest = dataset()
    second, _, _ = dataset()
    directory = settings.uploads_dir / first
    if kind == "parent":
        manifest["tables"][0]["file"] = f"../{second}/source.parquet"
    elif kind == "absolute":
        manifest["tables"][0]["file"] = str(settings.uploads_dir / second / "source.parquet")
    elif kind == "symlink":
        (directory / "source.parquet").unlink()
        (directory / "source.parquet").symlink_to(settings.uploads_dir / second / "source.parquet")
    elif kind == "directory":
        directory.rename(settings.uploads_dir / "saved")
        directory.symlink_to(settings.uploads_dir / "saved", target_is_directory=True)
    elif kind == "manifest":
        (directory / "manifest.json").unlink()
        (directory / "manifest.json").symlink_to(settings.uploads_dir / second / "manifest.json")
    if kind in {"parent", "absolute"}:
        (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ConnectorError, match="manifest"):
        database.list_schemas(first)


def test_connection_is_read_only_and_configuration_locked(database, dataset, tmp_path):
    dataset_id, _, _ = dataset()
    with database.connect(dataset_id) as conn:
        row = conn.execute("""SELECT current_setting('access_mode'),
            current_setting('autoinstall_known_extensions'),
            current_setting('autoload_known_extensions'),
            current_setting('enable_external_access')""").fetchone()
        assert row == ("read_only", False, False, False)
        for sql in [
            "CREATE TABLE bad(id INT)",
            "INSTALL httpfs",
            "LOAD httpfs",
            "SET enable_external_access = true",
            "SET allowed_paths = ['/etc/passwd']",
            f"COPY (SELECT 1) TO '{tmp_path}/outside.csv'",
        ]:
            with pytest.raises(duckdb.Error):
                conn.execute(sql)
    assert not (tmp_path / "outside.csv").exists()


async def test_streamable_http_tools_validation_and_concurrent_isolation(mcp_base, dataset):
    first, schema, _ = dataset()
    second, _, _ = dataset("SELECT 2 AS other", table="other")
    results = await asyncio.gather(
        *[check(f"{mcp_base}/datasets/{dataset_id}/mcp") for dataset_id in [first, second]]
    )
    assert results[0]["tables"] == ["orders"]
    assert results[1]["tables"] == ["other"]
    async with Client(f"{mcp_base}/datasets/{first}/mcp") as client:
        for limit in [0, 101, True, "10"]:
            result = await client.call_tool(
                "sample_rows", {"schema": schema, "table": "orders", "limit": limit}
            )
            assert result.is_error
        result = await client.call_tool("sample_rows", {"schema": schema, "table": "other"})
        assert result.is_error
    async with httpx.AsyncClient() as client:
        assert (await client.get(f"{mcp_base}/healthz")).json() == {"status": "ok"}
        for path in ["not-uuid", "..%2f..%2fetc", "%2e%2e"]:
            assert (
                await client.post(f"{mcp_base}/datasets/{path}/mcp", json={})
            ).status_code == 404
        assert (
            await client.post(
                f"{mcp_base}/datasets/{first}/mcp", headers={"host": "attacker.invalid"}, json={}
            )
        ).status_code == 421
