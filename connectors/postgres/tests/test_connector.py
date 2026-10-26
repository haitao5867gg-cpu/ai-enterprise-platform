from typing import Any
from uuid import uuid4

import httpx2 as httpx
import psycopg
import pytest
from mcp import Client
from psycopg import AsyncConnection, sql
from pydantic import ValidationError

from aep_postgres.config import Settings, validate_identifier
from aep_postgres.database import ConnectorError, Database
from aep_postgres.server import create_server
from aep_postgres.smoke import check


async def test_seed_data_and_metadata(database: Database) -> None:
    assert (await database.list_schemas())["schemas"] == ["sales"]
    assert (await database.list_tables("sales"))["tables"] == ["customers", "orders", "products"]
    expected = {"customers": 1000, "products": 200, "orders": 5000}
    for table, count in expected.items():
        stats = await database.table_stats("sales", table)
        assert stats["estimated_row_count"] == count
        assert stats["sample_row_count"] == min(count, stats["sample_limit"])
        assert all(c["name"] for c in stats["columns"])
        assert any(0 < (c["null_fraction"] or 0) < 1 for c in stats["columns"])

    description = await database.describe_table("sales", "orders")
    columns = {c["name"]: c for c in description["columns"]}
    assert columns["quantity"] == {
        "name": "quantity",
        "data_type": "integer",
        "nullable": True,
        "primary_key": False,
    }
    assert {c["name"] for c in columns.values() if c["primary_key"]} == {"tenant_id", "order_id"}
    assert not columns["tenant_id"]["nullable"]
    foreign_keys = {fk["referenced_table"]: fk for fk in description["foreign_keys"]}
    for target, column in (("customers", "customer_id"), ("products", "product_id")):
        assert foreign_keys[target]["columns"] == ["tenant_id", column]
        assert foreign_keys[target]["referenced_schema"] == "sales"
        assert foreign_keys[target]["referenced_columns"] == ["tenant_id", column]
    assert len((await database.sample_rows("sales", "orders"))["rows"]) == 10
    assert len((await database.sample_rows("sales", "orders", 100))["rows"]) == 100


async def test_quoted_identifiers_types_and_exact_sample_statistics(
    database: Database, odd_tables: tuple[str, str]
) -> None:
    schema, table = odd_tables
    assert (await database.list_tables(schema))["tables"] == [table, "empty"]
    rows = (await database.sample_rows(schema, table, 100))["rows"]
    assert len(rows) == 3
    assert rows[0]["id 'column"] == 1
    assert rows[0]["payload"] == {"a": 1}
    assert rows[0]["amount"] == 12.5
    assert rows[0]["created_at"].startswith("2026-01-01")
    assert rows[0]["t"] == "shadow"  # A column named like the SQL table alias still returns a row.
    description = await database.describe_table(schema, table)
    assert description["columns"][0]["primary_key"]
    assert description["columns"][-1]["name"] == "t"
    assert not description["columns"][-1]["nullable"]  # NOT NULL domains count as non-nullable.
    stats = await database.table_stats(schema, table)
    assert stats["estimated_row_count"] == stats["sample_row_count"] == 3
    columns = {c["name"]: c for c in stats["columns"]}
    assert columns["nullable"]["null_fraction"] == pytest.approx(1 / 3)
    assert columns["nullable"]["distinct_count"] == 2  # NULL excluded; empty string included.
    assert columns["payload"]["distinct_count"] == 1  # json has no native equality operator.
    assert columns["id 'column"]["distinct_count"] == 3
    empty = await database.table_stats(schema, "empty")
    assert empty["estimated_row_count"] == empty["sample_row_count"] == 0
    assert all(c["null_fraction"] is None and c["distinct_count"] == 0 for c in empty["columns"])
    assert (await database.sample_rows(schema, "empty"))["rows"] == []


@pytest.mark.parametrize("table", ["hidden", "a_view", "missing"])
async def test_inaccessible_and_non_base_tables_are_rejected(
    database: Database, odd_tables: tuple[str, str], table: str
) -> None:
    for method in (database.sample_rows, database.describe_table, database.table_stats):
        with pytest.raises(ConnectorError, match="does not exist or SELECT"):
            await method(odd_tables[0], table)


@pytest.mark.parametrize("name", ["pg_catalog", "information_schema", "pg_toast"])
async def test_system_schemas_are_not_exposed(database: Database, name: str) -> None:
    assert (await database.list_tables(name))["tables"] == []
    with pytest.raises(ConnectorError):
        await database.sample_rows(name, "pg_class")


@pytest.mark.parametrize(
    "name", ["orders; DROP TABLE sales.orders; --", 'orders"; DELETE FROM sales.orders; --']
)
async def test_injected_identifiers_cannot_execute_sql(database: Database, name: str) -> None:
    assert (await database.list_tables(name))["tables"] == []
    for method in (database.sample_rows, database.describe_table, database.table_stats):
        with pytest.raises(ConnectorError):
            await method("sales", name)
        with pytest.raises(ConnectorError):
            await method(name, "orders")
    assert (await database.table_stats("sales", "orders"))["estimated_row_count"] == 5000


@pytest.mark.parametrize(
    "limit", [0, -1, 101, 1000000, True, 1.5, "10", "1; DELETE FROM sales.orders"]
)
async def test_sample_limit_is_strict(database: Database, limit: Any) -> None:
    with pytest.raises(ConnectorError, match="limit"):
        await database.sample_rows("sales", "orders", limit)


@pytest.mark.parametrize("identifier", ["", "a\x00b", "a\nb", "a" * 64, "中" * 22])
def test_invalid_identifiers_and_database_names(identifier: str) -> None:
    with pytest.raises(ValueError):
        validate_identifier(identifier)
    with pytest.raises(ValidationError):
        Settings(postgres_database=identifier)


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO sales.customers (customer_id, name) VALUES (999999, 'blocked')",
        "UPDATE sales.customers SET name = 'blocked' WHERE customer_id = 1",
        "DELETE FROM sales.orders WHERE order_id = 1",
        "TRUNCATE sales.orders",
        "CREATE TABLE sales.blocked (id int)",
        "CREATE TEMP TABLE blocked (id int)",
        "ALTER TABLE sales.customers ADD COLUMN blocked int",
        "DROP TABLE sales.orders",
        "SELECT * INTO sales.blocked FROM sales.customers",
        "WITH deleted AS (DELETE FROM sales.orders RETURNING *) SELECT * FROM deleted",
        "GRANT INSERT ON sales.orders TO source_reader",
    ],
)
async def test_write_sql_is_rejected_by_read_only_transactions(
    database: Database, query: str
) -> None:
    async with database.connect() as conn:
        for setting in ("default_transaction_read_only", "transaction_read_only"):
            result = await (
                await conn.execute(sql.SQL("SHOW {}").format(sql.Identifier(setting)))
            ).fetchone()
            assert result and result[setting] == "on"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction) as error:
            async with conn.transaction():
                await conn.execute(query)
        assert error.value.sqlstate == "25006"
        result = await (await conn.execute("SELECT count(*) AS n FROM sales.orders")).fetchone()
        assert result and result["n"] == 5000


@pytest.mark.parametrize(
    "query",
    [
        "INSERT INTO sales.customers (customer_id, name) VALUES (999999, 'blocked')",
        "UPDATE sales.customers SET name = 'blocked' WHERE customer_id = 1",
        "DELETE FROM sales.orders WHERE order_id = 1",
        "TRUNCATE sales.orders",
        "CREATE TABLE sales.blocked (id int)",
        "CREATE TEMP TABLE blocked (id int)",
        "ALTER TABLE sales.customers ADD COLUMN blocked int",
        "DROP TABLE sales.orders",
    ],
)
async def test_account_permissions_block_writes_even_if_read_only_is_disabled(
    settings: Settings, query: str
) -> None:
    async with await psycopg.AsyncConnection.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        dbname=settings.postgres_database,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
        options="-c default_transaction_read_only=off",
        autocommit=True,
    ) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as error:
            await conn.execute(query)
        assert error.value.sqlstate == "42501"


async def test_slow_queries_time_out(settings: Settings) -> None:
    database = Database(settings.model_copy(update={"postgres_query_timeout_ms": 100}))
    with pytest.raises(ConnectorError, match="timeout"):
        async with database.connect() as conn:
            await conn.execute("SELECT pg_sleep(1)")


async def test_copy_from_is_rejected(database: Database) -> None:
    async with database.connect() as conn:
        # COPY checks table ACLs before its read-only-transaction check.
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as error:
            async with conn.transaction(), conn.cursor().copy("COPY sales.orders FROM STDIN"):
                pass
        assert error.value.sqlstate == "42501"


async def test_wide_table_and_missing_row_estimate(
    database: Database, admin: AsyncConnection[dict[str, Any]], odd_tables: tuple[str, str]
) -> None:
    table = sql.Identifier(odd_tables[0], "wide")
    columns = sql.SQL(", ").join(
        sql.SQL("{} integer").format(sql.Identifier(f"column_{index}")) for index in range(101)
    )
    await admin.execute(sql.SQL("CREATE TABLE {} ({})").format(table, columns))
    await admin.execute(sql.SQL("INSERT INTO {} DEFAULT VALUES").format(table))
    await admin.execute(sql.SQL("GRANT SELECT ON {} TO source_reader").format(table))
    stats = await database.table_stats(odd_tables[0], "wide")
    assert stats["estimated_row_count"] is None
    assert stats["sample_row_count"] == 1
    assert len(stats["columns"]) == 101
    assert all(c["null_fraction"] == 1 and c["distinct_count"] == 0 for c in stats["columns"])


async def test_superuser_configuration_is_refused(settings: Settings) -> None:
    from conftest import SeedSettings

    database = Database(
        settings.model_copy(
            update={
                "postgres_user": "source_admin",
                "postgres_password": SeedSettings().postgres_admin_password,
            }
        )
    )
    with pytest.raises(ConnectorError, match="SELECT-only"):
        await database.list_schemas()


@pytest.mark.parametrize(
    ("grant", "revoke"),
    [
        (
            "GRANT UPDATE ON sales.customers TO source_reader",
            "REVOKE UPDATE ON sales.customers FROM source_reader",
        ),
        (
            "GRANT UPDATE (name) ON sales.customers TO source_reader",
            "REVOKE UPDATE (name) ON sales.customers FROM source_reader",
        ),
        (
            "GRANT MAINTAIN ON sales.customers TO source_reader",
            "REVOKE MAINTAIN ON sales.customers FROM source_reader",
        ),
    ],
)
async def test_new_write_grant_is_detected(
    database: Database, admin: AsyncConnection[dict[str, Any]], grant: str, revoke: str
) -> None:
    await admin.execute(grant)
    try:
        with pytest.raises(ConnectorError, match="SELECT-only"):
            await database.list_schemas()
    finally:
        await admin.execute(revoke)
    assert "sales" in (await database.list_schemas())["schemas"]


async def test_non_inherited_but_assumable_write_role_is_refused(
    database: Database, admin: AsyncConnection[dict[str, Any]]
) -> None:
    role = sql.Identifier("connector_writer_" + uuid4().hex)
    await admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
    try:
        await admin.execute(sql.SQL("GRANT UPDATE ON sales.customers TO {}").format(role))
        await admin.execute(sql.SQL("GRANT {} TO source_reader").format(role))
        with pytest.raises(ConnectorError, match="SELECT-only"):
            await database.list_schemas()
    finally:
        await admin.execute(sql.SQL("DROP OWNED BY {}").format(role))
        await admin.execute(sql.SQL("DROP ROLE {}").format(role))


@pytest.mark.parametrize(
    "name", ["pg_write_server_files", "pg_execute_server_program", "pg_maintain"]
)
async def test_privileged_server_roles_are_refused(
    database: Database, admin: AsyncConnection[dict[str, Any]], name: str
) -> None:
    role = sql.Identifier(name)
    await admin.execute(sql.SQL("GRANT {} TO source_reader").format(role))
    try:
        with pytest.raises(ConnectorError, match="SELECT-only"):
            await database.list_schemas()
    finally:
        await admin.execute(sql.SQL("REVOKE {} FROM source_reader").format(role))


async def test_readiness_reports_source_failure(settings: Settings) -> None:
    settings = settings.model_copy(update={"postgres_port": 1})
    app = create_server(settings).streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.get("/healthz")
        assert response.status_code == 503
        assert response.json() == {"status": "error"}


async def test_all_tools_over_streamable_http(mcp_url: str) -> None:
    result = await check(mcp_url)
    assert result["status"] == "ok"


async def test_mcp_errors_and_readiness(mcp_url: str) -> None:
    async with Client(mcp_url) as client:
        for limit in (0, 101, True, "10"):
            result = await client.call_tool(
                "sample_rows", {"schema": "sales", "table": "orders", "limit": limit}
            )
            assert result.is_error
        result = await client.call_tool("sample_rows", {"schema": "sales", "table": "missing"})
        assert result.is_error
        assert "does not exist or SELECT" in str(result.content)
        assert "local-source-reader" not in str(result.content)
    async with httpx.AsyncClient() as client:
        health = await client.get(mcp_url.replace("/mcp", "/healthz"))
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        rebound = await client.post(mcp_url, headers={"host": "attacker.invalid"}, json={})
        assert rebound.status_code == 421


@pytest.mark.parametrize(
    "settings",
    [
        Settings(
            postgres_mcp_allowed_hosts=["proxy.example"],
            postgres_mcp_allowed_origins=["https://proxy.example"],
        )
    ],
)
async def test_proxy_host_and_origin_are_configurable(mcp_url: str) -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "proxy-test", "version": "1"},
        },
    }
    headers = {
        "accept": "application/json, text/event-stream",
        "host": "proxy.example",
        "origin": "https://proxy.example",
    }
    async with httpx.AsyncClient() as client:
        rejected = await client.post(mcp_url, json=payload)
        assert rejected.status_code == 421
        allowed = await client.post(mcp_url, headers=headers, json=payload)
        assert allowed.status_code == 200
        assert allowed.json()["result"]["serverInfo"]["name"] == "aep-postgres"
        bad_origin = await client.post(
            mcp_url, headers={**headers, "origin": "https://attacker.invalid"}, json=payload
        )
        assert bad_origin.status_code == 403
