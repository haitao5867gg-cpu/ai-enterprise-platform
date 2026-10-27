from typing import Any
from uuid import uuid4

import aiomysql
import httpx2 as httpx
import pytest
from aiomysql import Connection
from conftest import SeedSettings, query
from mcp import Client
from pydantic import ValidationError
from pymysql import MySQLError

from aep_mysql.config import Settings, quote_identifier, validate_identifier
from aep_mysql.database import ConnectorError, Database, _read_only_grant
from aep_mysql.server import create_server
from aep_mysql.smoke import check


async def test_seed_data_and_metadata(database: Database, admin: Connection) -> None:
    assert (await database.list_schemas())["schemas"] == ["sales"]
    assert (await database.list_tables("sales"))["tables"] == ["customers", "orders", "products"]
    for table, count in {"customers": 1000, "products": 200, "orders": 5000}.items():
        [actual] = await query(admin, f"SELECT count(*) AS n FROM sales.{quote_identifier(table)}")
        assert actual["n"] == count
        stats = await database.table_stats("sales", table)
        # InnoDB row estimates are approximate, including after ANALYZE TABLE.
        assert isinstance(stats["estimated_row_count"], int) and stats["estimated_row_count"] > 0
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
    assert (await database.list_tables(schema))["tables"] == [table, "empty"]
    rows = (await database.sample_rows(schema, table, 100))["rows"]
    assert len(rows) == 3
    assert rows[0]["id ` '列; --"] == 1
    assert rows[0]["payload"] == {"a": 1}
    assert rows[0]["amount"] == 12.5
    assert rows[0]["created_at"] == "2026-01-01T00:00:00"
    assert rows[0]["t"] == "shadow"
    assert rows[0]["binary_value"] == "\\x00ff"
    description = await database.describe_table(schema, table)
    assert description["columns"][0]["primary_key"] is True
    assert description["columns"][-2]["name"] == "t"
    assert description["columns"][-2]["nullable"] is False
    stats = await database.table_stats(schema, table)
    assert stats["sample_row_count"] == 3
    columns = {c["name"]: c for c in stats["columns"]}
    assert columns["nullable"]["null_fraction"] == pytest.approx(1 / 3)
    assert columns["nullable"]["distinct_count"] == 2
    assert columns["payload"]["distinct_count"] == 1
    assert columns["id ` '列; --"]["distinct_count"] == 3
    assert columns["nullable"]["top_values"] == [
        {"value": "", "count": 1},
        {"value": "x", "count": 1},
    ]
    assert columns["payload"]["top_values"] == [{"value": '{"a":1}', "count": 2}]
    assert columns["t"]["top_values"] == [{"value": "shadow", "count": 3}]
    empty = await database.table_stats(schema, "empty")
    assert empty["sample_row_count"] == 0
    assert all(c["null_fraction"] is None and c["distinct_count"] == 0 for c in empty["columns"])
    assert all("top_values" not in c for c in empty["columns"])
    assert (await database.sample_rows(schema, "empty"))["rows"] == []


async def test_unique_not_null_is_not_reported_as_a_primary_key(
    database: Database, admin: Connection, odd_tables: tuple[str, str]
) -> None:
    schema, _ = odd_tables
    table = f"{quote_identifier(schema)}.unique_only"
    await query(admin, f"CREATE TABLE {table} (id int UNIQUE NOT NULL, value text)")
    await query(admin, f"GRANT SELECT ON {table} TO 'source_reader'@'%'")
    description = await database.describe_table(schema, "unique_only")
    assert all(not column["primary_key"] for column in description["columns"])


@pytest.mark.parametrize("table", ["hidden", "a_view", "column_only", "missing"])
async def test_inaccessible_and_non_base_tables_are_rejected(
    database: Database, odd_tables: tuple[str, str], table: str
) -> None:
    for method in (database.sample_rows, database.describe_table, database.table_stats):
        with pytest.raises(ConnectorError, match="does not exist or SELECT"):
            await method(odd_tables[0], table)


@pytest.mark.parametrize("name", ["mysql", "information_schema", "performance_schema", "sys"])
async def test_system_schemas_are_not_exposed(database: Database, name: str) -> None:
    assert (await database.list_tables(name))["tables"] == []
    with pytest.raises(ConnectorError):
        await database.sample_rows(name, "user")


@pytest.mark.parametrize(
    "name", ["orders; DROP TABLE sales.orders; --", "orders`; DELETE FROM sales.orders; --"]
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
    "identifier", ["", "a\x00b", "a\nb", "a" * 65, "中" * 65, "tail ", "😀", "\ud800"]
)
def test_invalid_identifiers_and_database_names(identifier: str) -> None:
    with pytest.raises(ValueError):
        validate_identifier(identifier)
    with pytest.raises(ValidationError):
        Settings(mysql_database=identifier)


def test_backticks_are_escaped_and_unicode_uses_character_limit() -> None:
    assert quote_identifier("库`表'名") == "`库``表'名`"
    assert validate_identifier("中" * 64) == "中" * 64


WRITES = [
    "INSERT INTO sales.customers (customer_id, name) VALUES (999999, 'blocked')",
    "UPDATE sales.customers SET name = 'blocked' WHERE customer_id = 1",
    "DELETE FROM sales.orders WHERE order_id = 1",
    "TRUNCATE sales.orders",
    "CREATE TABLE sales.blocked (id int)",
    "CREATE TEMPORARY TABLE sales.blocked (id int)",
    "ALTER TABLE sales.customers ADD COLUMN blocked int",
    "DROP TABLE sales.orders",
]


@pytest.mark.parametrize("sql", WRITES)
async def test_connector_sessions_are_read_only_and_reject_writes(
    database: Database, sql: str
) -> None:
    async with database.connect() as conn:
        [row] = await query(conn, "SELECT @@session.transaction_read_only AS ro")
        assert row["ro"] == 1
        with pytest.raises(MySQLError) as error:
            await query(conn, sql)
        assert error.value.args[0] in (1142, 1792)


@pytest.mark.parametrize("sql", WRITES)
async def test_account_permissions_block_writes_with_read_only_disabled(
    settings: Settings, sql: str
) -> None:
    conn = await aiomysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        db=settings.mysql_database,
        user=settings.mysql_user,
        password=settings.mysql_password.get_secret_value(),
        autocommit=True,
    )
    try:
        await query(conn, "SET SESSION TRANSACTION READ WRITE")
        with pytest.raises(MySQLError) as error:
            await query(conn, sql)
        assert error.value.args[0] in (1044, 1142)
    finally:
        conn.close()


async def test_read_only_transaction_still_blocks_a_later_write_grant(
    database: Database, admin: Connection
) -> None:
    async with database.connect() as conn:
        await query(admin, "GRANT UPDATE ON sales.customers TO 'source_reader'@'%'")
        try:
            with pytest.raises(MySQLError) as error:
                await query(
                    conn, "UPDATE sales.customers SET name = 'blocked' WHERE customer_id = 1"
                )
            assert error.value.args[0] == 1792
        finally:
            await query(admin, "REVOKE UPDATE ON sales.customers FROM 'source_reader'@'%'")


async def test_slow_queries_time_out(settings: Settings) -> None:
    database = Database(settings.model_copy(update={"mysql_query_timeout_ms": 100}))
    with pytest.raises(ConnectorError, match="timeout"):
        async with database.connect() as conn:
            [row] = await query(conn, "SELECT @@session.max_execution_time AS timeout")
            assert row["timeout"] == 100
            await database._query(conn, "SELECT SLEEP(1)")


async def test_stats_sample_bound_and_wide_table(
    settings: Settings, admin: Connection, odd_tables: tuple[str, str]
) -> None:
    database = Database(settings.model_copy(update={"mysql_stats_sample_size": 2}))
    assert (await database.table_stats(*odd_tables))["sample_row_count"] == 2
    table = f"{quote_identifier(odd_tables[0])}.wide"
    columns = ", ".join(f"{quote_identifier(f'column_{i}')} int" for i in range(101))
    await query(admin, f"CREATE TABLE {table} ({columns})")
    await query(admin, f"INSERT INTO {table} () VALUES ()")
    await query(admin, f"GRANT SELECT ON {table} TO 'source_reader'@'%'")
    stats = await database.table_stats(odd_tables[0], "wide")
    assert stats["sample_row_count"] == 1
    assert len(stats["columns"]) == 101
    assert all(c["null_fraction"] == 1 and c["distinct_count"] == 0 for c in stats["columns"])


async def test_invisible_columns_are_sampled(
    database: Database, admin: Connection, odd_tables: tuple[str, str]
) -> None:
    schema = odd_tables[0]
    table = f"{quote_identifier(schema)}.{quote_identifier('invisible %s')}"
    await query(admin, f"CREATE TABLE {table} (id int, `secret %` text INVISIBLE)")
    await query(admin, f"INSERT INTO {table} (id, `secret %`) VALUES (1, 'x'), (2, NULL)")
    await query(admin, f"GRANT SELECT ON {table} TO 'source_reader'@'%'")
    rows = (await database.sample_rows(schema, "invisible %s"))["rows"]
    assert rows == [{"id": 1, "secret %": "x"}, {"id": 2, "secret %": None}]
    stats = await database.table_stats(schema, "invisible %s")
    columns = {c["name"]: c for c in stats["columns"]}
    assert columns["secret %"]["null_fraction"] == 0.5
    assert columns["secret %"]["top_values"] == [{"value": "x", "count": 1}]


def writer_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"mysql_user": "root", "mysql_password": SeedSettings().mysql_admin_password}
    )


async def test_admin_configuration_is_refused(settings: Settings) -> None:
    with pytest.raises(ConnectorError, match="SELECT-only"):
        await Database(writer_settings(settings)).list_schemas()


async def test_writable_configuration_refuses_server_startup(settings: Settings) -> None:
    app = create_server(writer_settings(settings)).streamable_http_app()
    with pytest.raises(ConnectorError, match="SELECT-only"):
        async with app.router.lifespan_context(app):
            pytest.fail("Writable connector must not start")


@pytest.mark.parametrize(
    ("privilege", "scope"),
    [
        ("UPDATE", "sales.customers"),
        ("UPDATE (name)", "sales.customers"),
        ("INSERT", "sales.*"),
        ("CREATE TEMPORARY TABLES", "sales.*"),
        ("EXECUTE", "sales.*"),
        ("EVENT", "sales.*"),
        ("FILE", "*.*"),
        ("PROCESS", "*.*"),
        ("SYSTEM_USER", "*.*"),
    ],
)
async def test_new_write_or_admin_grants_are_detected(
    database: Database, admin: Connection, privilege: str, scope: str
) -> None:
    await query(admin, f"GRANT {privilege} ON {scope} TO 'source_reader'@'%'")
    try:
        with pytest.raises(ConnectorError, match="SELECT-only"):
            await database.list_schemas()
    finally:
        await query(admin, f"REVOKE {privilege} ON {scope} FROM 'source_reader'@'%'")
    assert "sales" in (await database.list_schemas())["schemas"]


async def test_grant_option_is_refused(database: Database, admin: Connection) -> None:
    await query(admin, "GRANT SELECT ON sales.* TO 'source_reader'@'%' WITH GRANT OPTION")
    try:
        with pytest.raises(ConnectorError, match="SELECT-only"):
            await database.list_schemas()
    finally:
        await query(admin, "REVOKE GRANT OPTION ON sales.* FROM 'source_reader'@'%'")


async def test_non_default_write_role_is_refused(database: Database, admin: Connection) -> None:
    role = quote_identifier("connector_writer_" + uuid4().hex[:12])
    await query(admin, f"CREATE ROLE {role}")
    try:
        await query(admin, f"GRANT UPDATE ON sales.customers TO {role}")
        await query(admin, f"GRANT {role} TO 'source_reader'@'%'")
        with pytest.raises(ConnectorError, match="SELECT-only"):
            await database.list_schemas()
    finally:
        await query(admin, f"DROP ROLE {role}")


async def test_mandatory_roles_are_refused(database: Database, admin: Connection) -> None:
    role = "connector_mandatory_" + uuid4().hex[:12]
    [original] = await query(admin, "SELECT @@GLOBAL.mandatory_roles AS roles")
    await query(admin, f"CREATE ROLE {quote_identifier(role)}")
    try:
        await query(admin, f"GRANT UPDATE ON sales.customers TO {quote_identifier(role)}")
        await query(admin, "SET GLOBAL mandatory_roles = %s", (role,))
        with pytest.raises(ConnectorError, match="SELECT-only"):
            await database.list_schemas()
    finally:
        await query(admin, "SET GLOBAL mandatory_roles = %s", (original["roles"],))
        await query(admin, f"DROP ROLE {quote_identifier(role)}")


@pytest.mark.parametrize(
    "grant",
    [
        "GRANT UPDATE (`SELECT`) ON `sales`.`t` TO `r`@`%`",
        "GRANT `SELECT ON x`@`%` TO `r`@`%`",  # A misleading role name.
        "GRANT PROXY ON `root`@`localhost` TO `r`@`%`",
        "GRANT SELECT ON *.* TO `r`@`%` WITH GRANT OPTION",
    ],
)
def test_grant_parser_fails_closed(grant: str) -> None:
    assert not _read_only_grant(grant)


async def test_top_values_count_whole_values_then_truncate(
    database: Database, admin: Connection, odd_tables: tuple[str, str]
) -> None:
    schema, _ = odd_tables
    prefix = "中" * 80
    table = f"{quote_identifier(schema)}.empty"
    for i, value in enumerate(
        [prefix + "a", prefix + "b", prefix + "b", "short", "exact" + "x" * 75]
    ):
        await query(admin, f"INSERT INTO {table} VALUES (%s, %s)", (i, value))
    stats = await database.table_stats(schema, "empty")
    [column] = [c for c in stats["columns"] if c["name"] == "value"]
    assert column["top_values"] == [
        {"value": prefix, "count": 2, "truncated": True},
        {"value": "exact" + "x" * 75, "count": 1},
        {"value": "short", "count": 1},
        {"value": prefix, "count": 1, "truncated": True},
    ]


async def test_top_values_preserve_case_and_spaces(
    database: Database, admin: Connection, odd_tables: tuple[str, str]
) -> None:
    schema, _ = odd_tables
    table = f"{quote_identifier(schema)}.empty"
    for i, value in enumerate(["paid", "PAID", "paid ", "paid", ""]):
        await query(admin, f"INSERT INTO {table} VALUES (%s, %s)", (i, value))
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
    database: Database, admin: Connection, odd_tables: tuple[str, str]
) -> None:
    schema, _ = odd_tables
    table = f"{quote_identifier(schema)}.empty"
    for i in range(21):
        await query(admin, f"INSERT INTO {table} VALUES (%s, %s)", (i, str(i % 20)))
    columns = {c["name"]: c for c in (await database.table_stats(schema, "empty"))["columns"]}
    assert columns["id"]["distinct_count"] == 21 and "top_values" not in columns["id"]
    assert columns["value"]["distinct_count"] == len(columns["value"]["top_values"]) == 20


async def test_readiness_reports_source_failure(settings: Settings) -> None:
    app = create_server(settings.model_copy(update={"mysql_port": 1})).streamable_http_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.get("/healthz")
        assert response.status_code == 503 and response.json() == {"status": "error"}


async def test_all_tools_over_streamable_http(mcp_url: str) -> None:
    assert (await check(mcp_url))["status"] == "ok"


async def test_mcp_errors_and_readiness(mcp_url: str) -> None:
    async with Client(mcp_url) as client:
        for limit in (0, 101, True, "10"):
            result = await client.call_tool(
                "sample_rows", {"schema": "sales", "table": "orders", "limit": limit}
            )
            assert result.is_error
        result = await client.call_tool("sample_rows", {"schema": "sales", "table": "missing"})
        assert result.is_error and "does not exist or SELECT" in str(result.content)
        assert "local-source-reader" not in str(result.content)
    async with httpx.AsyncClient() as client:
        health = await client.get(mcp_url.replace("/mcp", "/healthz"))
        assert health.status_code == 200 and health.json() == {"status": "ok"}
        rebound = await client.post(mcp_url, headers={"host": "attacker.invalid"}, json={})
        assert rebound.status_code == 421
