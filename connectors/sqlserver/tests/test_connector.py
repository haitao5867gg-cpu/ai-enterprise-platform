from typing import Any
from uuid import uuid4

import httpx2 as httpx
import pymssql
import pytest
from conftest import drop_schema, grant, query
from mcp import Client
from pydantic import ValidationError

from aep_sqlserver.config import Settings, quote_identifier, validate_identifier
from aep_sqlserver.database import ConnectorError, Database, data_type
from aep_sqlserver.seed import SeedSettings, batches
from aep_sqlserver.server import create_server
from aep_sqlserver.smoke import check


async def test_seed_data_and_metadata(database: Database, admin: Any) -> None:
    assert (await database.list_schemas())["schemas"] == ["sales"]
    assert (await database.list_tables("sales"))["tables"] == ["customers", "orders", "products"]
    for table, count in {"customers": 1000, "products": 200, "orders": 5000}.items():
        [actual] = query(admin, f"SELECT count(*) AS n FROM sales.{quote_identifier(table)}")
        assert actual["n"] == count
        stats = await database.table_stats("sales", table)
        assert stats["estimated_row_count"] == count
        assert stats["sample_row_count"] == min(count, stats["sample_limit"])
        assert any(0 < (c["null_fraction"] or 0) < 1 for c in stats["columns"])

    orders = {c["name"]: c for c in (await database.table_stats("sales", "orders"))["columns"]}
    assert {v["value"] for v in orders["status"]["top_values"]} == {"paid", " Paid "}
    assert "top_values" not in orders["order_id"]
    description = await database.describe_table("sales", "orders")
    columns = {c["name"]: c for c in description["columns"]}
    assert columns["quantity"] == {
        "name": "quantity",
        "data_type": "int",
        "nullable": True,
        "primary_key": False,
    }
    assert columns["status"]["data_type"] == "nvarchar(20)"
    assert columns["ordered_at"]["data_type"] == "datetime2(0)"
    assert {c["name"] for c in columns.values() if c["primary_key"]} == {"tenant_id", "order_id"}
    assert not columns["tenant_id"]["nullable"]
    foreign_keys = {fk["referenced_table"]: fk for fk in description["foreign_keys"]}
    assert len(foreign_keys) == 2
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
    assert schema in (await database.list_schemas())["schemas"]
    # Ordered by the database collation (case-insensitive by default).
    assert (await database.list_tables(schema))["tables"] == ["empty", table]
    rows = (await database.sample_rows(schema, table, 100))["rows"]
    rows.sort(key=lambda row: row["id ] '列; --"])
    assert len(rows) == 3
    first = rows[0]
    assert first["id ] '列; --"] == 1
    assert first["payload"] == '{"a":1}'
    assert first["amount"] == 12.5
    assert first["created_at"] == "2026-01-01T00:00:00"
    assert first["t"] == "shadow"
    assert first["binary_value"] == "\\x00ff"
    assert first["uid"].lower() == "6f9619ff-8b86-d011-b42d-00c04fc964ff"
    assert first["flag"] is True
    assert first["place"] == "POINT (121.5 31.2)"
    assert first["doc"] == "<a>1</a>"
    description = await database.describe_table(schema, table)
    types = {c["name"]: c["data_type"] for c in description["columns"]}
    assert types["payload"] == "nvarchar(max)" and types["amount"] == "decimal(12,2)"
    assert types["binary_value"] == "varbinary(4)" and types["place"] == "geography"
    assert description["columns"][0]["primary_key"] is True
    assert description["columns"][5]["name"] == "t"
    assert description["columns"][5]["nullable"] is False
    stats = await database.table_stats(schema, table)
    assert stats["sample_row_count"] == 3
    columns = {c["name"]: c for c in stats["columns"]}
    assert columns["nullable"]["null_fraction"] == pytest.approx(1 / 3)
    assert columns["nullable"]["distinct_count"] == 2
    assert columns["payload"]["distinct_count"] == 1
    assert columns["id ] '列; --"]["distinct_count"] == 3
    assert columns["nullable"]["top_values"] == [
        {"value": "", "count": 1},
        {"value": "x", "count": 1},
    ]
    assert columns["t"]["top_values"] == [{"value": "shadow", "count": 3}]
    empty = await database.table_stats(schema, "empty")
    assert empty["sample_row_count"] == 0 and empty["estimated_row_count"] == 0
    assert all(c["null_fraction"] is None and c["distinct_count"] == 0 for c in empty["columns"])
    assert all("top_values" not in c for c in empty["columns"])
    assert (await database.sample_rows(schema, "empty"))["rows"] == []


def test_data_type_spelling() -> None:
    def spelled(name: str, length: int = 0, precision: int = 0, scale: int = 0) -> str:
        return data_type(
            {"type_name": name, "max_length": length, "precision": precision, "scale": scale}
        )

    assert spelled("nvarchar", 100) == "nvarchar(50)"
    assert spelled("varchar", -1) == "varchar(max)"
    assert spelled("numeric", 9, 18, 4) == "numeric(18,4)"
    assert spelled("datetimeoffset", 10, 34, 7) == "datetimeoffset(7)"
    assert spelled("int", 4, 10, 0) == "int"


async def test_unique_not_null_is_not_reported_as_a_primary_key(
    database: Database, admin: Any, odd_tables: tuple[str, str]
) -> None:
    schema, _ = odd_tables
    table = f"{quote_identifier(schema)}.unique_only"
    query(admin, f"CREATE TABLE {table} (id int UNIQUE NOT NULL, value nvarchar(10))")
    grant(admin, table)
    description = await database.describe_table(schema, "unique_only")
    assert all(not column["primary_key"] for column in description["columns"])


@pytest.mark.parametrize("table", ["hidden", "a_view", "column_only", "missing"])
async def test_inaccessible_and_non_base_tables_are_rejected(
    database: Database, odd_tables: tuple[str, str], table: str
) -> None:
    for method in (database.sample_rows, database.describe_table, database.table_stats):
        with pytest.raises(ConnectorError, match="does not exist or SELECT"):
            await method(odd_tables[0], table)


@pytest.mark.parametrize("name", ["sys", "INFORMATION_SCHEMA", "dbo", "guest"])
async def test_system_schemas_are_not_exposed(database: Database, name: str) -> None:
    assert (await database.list_tables(name))["tables"] == []
    assert name not in (await database.list_schemas())["schemas"]
    with pytest.raises(ConnectorError):
        await database.sample_rows(name, "objects")


@pytest.mark.parametrize(
    "name", ["orders; DROP TABLE sales.orders; --", "orders]; DELETE FROM sales.orders; --"]
)
async def test_injected_identifiers_cannot_execute_sql(database: Database, name: str) -> None:
    assert (await database.list_tables(name))["tables"] == []
    for method in (database.sample_rows, database.describe_table, database.table_stats):
        with pytest.raises(ConnectorError):
            await method("sales", name)
        with pytest.raises(ConnectorError):
            await method(name, "orders")
    assert len((await database.sample_rows("sales", "orders"))["rows"]) == 10


@pytest.mark.parametrize(
    "limit", [0, -1, 101, 1000000, True, 1.5, "10", "1; DELETE FROM sales.orders"]
)
async def test_sample_limit_is_strict(database: Database, limit: Any) -> None:
    with pytest.raises(ConnectorError, match="limit"):
        await database.sample_rows("sales", "orders", limit)


@pytest.mark.parametrize(
    "identifier", ["", "a\x00b", "a\nb", "a" * 129, "中" * 129, "tail ", " lead", "\ud800"]
)
def test_invalid_identifiers_and_database_names(identifier: str) -> None:
    with pytest.raises(ValueError):
        validate_identifier(identifier)
    with pytest.raises(ValidationError):
        Settings(sqlserver_database=identifier)


def test_brackets_are_escaped_and_unicode_uses_character_limit() -> None:
    assert quote_identifier("库]表'名") == "[库]]表'名]"
    assert validate_identifier("中" * 128) == "中" * 128


def test_seed_batches_split_on_go_lines() -> None:
    assert batches("CREATE A;\nGO\n  go  \nSELECT 'GO';\nGO") == ["CREATE A;", "SELECT 'GO';"]


WRITES = [
    "INSERT INTO sales.customers (customer_id, name) VALUES (999999, N'blocked')",
    "UPDATE sales.customers SET name = N'blocked' WHERE customer_id = 1",
    "DELETE FROM sales.orders WHERE order_id = 1",
    "TRUNCATE TABLE sales.orders",
    "CREATE TABLE sales.blocked (id int)",
    "ALTER TABLE sales.customers ADD blocked int",
    "DROP TABLE sales.orders",
    "EXEC sp_rename 'sales.orders', 'renamed'",
]


@pytest.mark.parametrize("sql", WRITES)
def test_account_permissions_block_writes(database: Database, sql: str) -> None:
    with database.connect() as conn:
        with pytest.raises(pymssql.Error):
            query(conn, sql)


def test_connector_sessions_roll_back_even_after_a_later_write_grant(
    database: Database, admin: Any
) -> None:
    with pytest.raises(ConnectorError):
        with database.connect() as conn:
            query(admin, "GRANT UPDATE ON sales.customers TO source_reader")
            try:
                query(conn, "UPDATE sales.customers SET name = N'blocked' WHERE customer_id = 1")
            finally:
                query(admin, "REVOKE UPDATE ON sales.customers FROM source_reader")
            # The grant arrived after the check: the write ran, but is rolled back below.
            raise pymssql.OperationalError("stop")
    [row] = query(admin, "SELECT name FROM sales.customers WHERE customer_id = 1")
    assert row["name"] == "Customer 1"


async def test_slow_queries_time_out(settings: Settings) -> None:
    database = Database(settings.model_copy(update={"sqlserver_query_timeout_seconds": 1}))
    with pytest.raises(ConnectorError, match="timeout"):
        with database.connect() as conn:
            [row] = query(conn, "SELECT @@LOCK_TIMEOUT AS timeout")
            assert row["timeout"] == 1000
            query(conn, "WAITFOR DELAY '00:00:03'")


async def test_stats_sample_bound_and_wide_table(
    settings: Settings, admin: Any, odd_tables: tuple[str, str]
) -> None:
    database = Database(settings.model_copy(update={"sqlserver_stats_sample_size": 2}))
    assert (await database.table_stats(*odd_tables))["sample_row_count"] == 2
    table = f"{quote_identifier(odd_tables[0])}.wide"
    columns = ", ".join(f"{quote_identifier(f'column_{i}')} int" for i in range(101))
    query(admin, f"CREATE TABLE {table} ({columns})")
    query(admin, f"INSERT INTO {table} DEFAULT VALUES")
    grant(admin, table)
    stats = await database.table_stats(odd_tables[0], "wide")
    assert stats["sample_row_count"] == 1
    assert len(stats["columns"]) == 101
    assert all(c["null_fraction"] == 1 and c["distinct_count"] == 0 for c in stats["columns"])


async def test_percent_signs_in_names_survive_parameter_binding(
    database: Database, admin: Any, odd_tables: tuple[str, str]
) -> None:
    schema = odd_tables[0]
    table = f"{quote_identifier(schema)}.{quote_identifier('pct %s %d')}"
    query(admin, f"CREATE TABLE {table} (id int, [secret %] nvarchar(10))")
    query(admin, f"INSERT INTO {table} VALUES (1, N'x'), (2, NULL)")
    grant(admin, table)
    rows = (await database.sample_rows(schema, "pct %s %d"))["rows"]
    assert sorted(rows, key=lambda row: row["id"]) == [
        {"id": 1, "secret %": "x"},
        {"id": 2, "secret %": None},
    ]
    page = await database.read_rows(schema, "pct %s %d", 0, 1)
    assert page["rows"] == [{"id": 1, "secret %": "x"}] and page["next_offset"] == 1


def admin_settings(settings: Settings) -> Settings:
    seed = SeedSettings()
    return settings.model_copy(
        update={
            "sqlserver_user": seed.sqlserver_admin_user,
            "sqlserver_password": seed.sqlserver_admin_password,
        }
    )


async def test_admin_configuration_is_refused(settings: Settings) -> None:
    with pytest.raises(ConnectorError, match="may only SELECT"):
        await Database(admin_settings(settings)).list_schemas()


async def test_writable_configuration_refuses_server_startup(settings: Settings) -> None:
    app = create_server(admin_settings(settings)).streamable_http_app()
    with pytest.raises(ConnectorError, match="may only SELECT"):
        async with app.router.lifespan_context(app):
            pytest.fail("Writable connector must not start")


@pytest.mark.parametrize(
    ("give", "take"),
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
            "GRANT INSERT ON SCHEMA::sales TO source_reader",
            "REVOKE INSERT ON SCHEMA::sales FROM source_reader",
        ),
        (
            "GRANT ALTER ON SCHEMA::sales TO source_reader",
            "REVOKE ALTER ON SCHEMA::sales FROM source_reader",
        ),
        ("GRANT EXECUTE TO source_reader", "REVOKE EXECUTE FROM source_reader"),
        ("GRANT CREATE TABLE TO source_reader", "REVOKE CREATE TABLE FROM source_reader"),
        (
            "GRANT IMPERSONATE ON USER::dbo TO source_reader",
            "REVOKE IMPERSONATE ON USER::dbo FROM source_reader",
        ),
        (
            "ALTER ROLE db_datawriter ADD MEMBER source_reader",
            "ALTER ROLE db_datawriter DROP MEMBER source_reader",
        ),
        (
            "ALTER ROLE db_owner ADD MEMBER source_reader",
            "ALTER ROLE db_owner DROP MEMBER source_reader",
        ),
        (
            "ALTER ROLE db_ddladmin ADD MEMBER source_reader",
            "ALTER ROLE db_ddladmin DROP MEMBER source_reader",
        ),
        (
            "EXEC ('USE master; GRANT VIEW SERVER STATE TO source_reader')",
            "EXEC ('USE master; REVOKE VIEW SERVER STATE FROM source_reader')",
        ),
        (
            "ALTER SERVER ROLE sysadmin ADD MEMBER source_reader",
            "ALTER SERVER ROLE sysadmin DROP MEMBER source_reader",
        ),
    ],
)
async def test_new_write_or_admin_grants_are_detected(
    database: Database, admin: Any, give: str, take: str
) -> None:
    query(admin, give)
    try:
        with pytest.raises(ConnectorError, match="may only SELECT"):
            await database.list_schemas()
    finally:
        query(admin, take)
    assert "sales" in (await database.list_schemas())["schemas"]


async def test_db_datareader_membership_is_allowed(database: Database, admin: Any) -> None:
    query(admin, "ALTER ROLE db_datareader ADD MEMBER source_reader")
    try:
        assert "sales" in (await database.list_schemas())["schemas"]
    finally:
        query(admin, "ALTER ROLE db_datareader DROP MEMBER source_reader")


async def test_a_custom_role_with_write_grants_is_refused(database: Database, admin: Any) -> None:
    role = quote_identifier("connector_writer_" + uuid4().hex[:12])
    query(admin, f"CREATE ROLE {role}")
    try:
        query(admin, f"GRANT UPDATE ON sales.customers TO {role}")
        query(admin, f"ALTER ROLE {role} ADD MEMBER source_reader")
        with pytest.raises(ConnectorError, match="may only SELECT"):
            await database.list_schemas()
    finally:
        query(admin, f"ALTER ROLE {role} DROP MEMBER source_reader")
        query(admin, f"DROP ROLE {role}")


async def test_owning_a_schema_is_refused(database: Database, admin: Any) -> None:
    schema = "connector_owned_" + uuid4().hex[:12]
    query(admin, f"CREATE SCHEMA {quote_identifier(schema)} AUTHORIZATION source_reader")
    try:
        with pytest.raises(ConnectorError, match="may only SELECT"):
            await database.list_schemas()
    finally:
        drop_schema(admin, schema)


async def test_top_values_count_whole_values_then_truncate(
    database: Database, admin: Any, odd_tables: tuple[str, str]
) -> None:
    schema, _ = odd_tables
    prefix = "中" * 80
    table = f"{quote_identifier(schema)}.empty"
    for i, value in enumerate(
        [prefix + "a", prefix + "b", prefix + "b", "short", "exact" + "x" * 75]
    ):
        query(admin, f"INSERT INTO {table} VALUES (%d, %s)", (i, value))
    stats = await database.table_stats(schema, "empty")
    [column] = [c for c in stats["columns"] if c["name"] == "value"]
    assert column["top_values"] == [
        {"value": prefix, "count": 2, "truncated": True},
        {"value": "exact" + "x" * 75, "count": 1},
        {"value": "short", "count": 1},
        {"value": prefix, "count": 1, "truncated": True},
    ]


async def test_top_values_preserve_case_and_spaces(
    database: Database, admin: Any, odd_tables: tuple[str, str]
) -> None:
    schema, _ = odd_tables
    table = f"{quote_identifier(schema)}.empty"
    for i, value in enumerate(["paid", "PAID", "paid ", "paid", ""]):
        query(admin, f"INSERT INTO {table} VALUES (%d, %s)", (i, value))
    stats = await database.table_stats(schema, "empty")
    [column] = [c for c in stats["columns"] if c["name"] == "value"]
    assert column["distinct_count"] == 4
    assert column["top_values"] == [
        {"value": "paid", "count": 2},
        {"value": "", "count": 1},
        {"value": "PAID", "count": 1},
        {"value": "paid ", "count": 1},
    ]


async def test_top_values_cardinality_boundary(
    database: Database, admin: Any, odd_tables: tuple[str, str]
) -> None:
    schema, _ = odd_tables
    table = f"{quote_identifier(schema)}.empty"
    for i in range(21):
        query(admin, f"INSERT INTO {table} VALUES (%d, %s)", (i, str(i % 20)))
    columns = {c["name"]: c for c in (await database.table_stats(schema, "empty"))["columns"]}
    assert columns["id"]["distinct_count"] == 21 and "top_values" not in columns["id"]
    assert columns["value"]["distinct_count"] == len(columns["value"]["top_values"]) == 20


async def test_readiness_reports_source_failure(settings: Settings) -> None:
    app = create_server(settings.model_copy(update={"sqlserver_port": 1})).streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.get("/healthz")
        assert response.status_code == 503 and response.json() == {"status": "error"}


async def test_all_tools_over_streamable_http(mcp_url: str) -> None:
    assert (await check(mcp_url))["status"] == "ok"


async def test_mcp_errors_and_readiness(mcp_url: str, settings: Settings) -> None:
    async with Client(mcp_url) as client:
        for limit in (0, 101, True, "10"):
            result = await client.call_tool(
                "sample_rows", {"schema": "sales", "table": "orders", "limit": limit}
            )
            assert result.is_error
        result = await client.call_tool("sample_rows", {"schema": "sales", "table": "missing"})
        assert result.is_error and "does not exist or SELECT" in str(result.content)
        assert settings.sqlserver_password.get_secret_value() not in str(result.content)
    async with httpx.AsyncClient() as client:
        health = await client.get(mcp_url.replace("/mcp", "/healthz"))
        assert health.status_code == 200 and health.json() == {"status": "ok"}
        rebound = await client.post(mcp_url, headers={"host": "attacker.invalid"}, json={})
        assert rebound.status_code == 421
