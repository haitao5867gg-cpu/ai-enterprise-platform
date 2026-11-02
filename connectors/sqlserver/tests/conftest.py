import asyncio
import socket
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any
from uuid import uuid4

import pymssql
import pytest
import uvicorn

from aep_sqlserver.config import Settings, quote_identifier
from aep_sqlserver.database import Database
from aep_sqlserver.seed import SeedSettings
from aep_sqlserver.server import create_server, http_security


def query(conn: Any, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cursor = conn.cursor(as_dict=True)
    try:
        if params:
            cursor.execute(sql, tuple(params))
        else:
            cursor.execute(sql)
        return list(cursor.fetchall()) if cursor.description else []
    finally:
        cursor.close()


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def database(settings: Settings) -> Database:
    return Database(settings)


@pytest.fixture
def admin(settings: Settings) -> Iterator[Any]:
    """Only the synthetic compose database is modified by these integration fixtures."""
    seed = SeedSettings()
    try:
        conn = pymssql.connect(
            server=settings.sqlserver_host,
            port=str(settings.sqlserver_port),
            user=seed.sqlserver_admin_user,
            password=seed.sqlserver_admin_password.get_secret_value(),
            database=settings.sqlserver_database,
            autocommit=True,
            login_timeout=5,
        )
    except pymssql.Error:
        pytest.fail(
            "Synthetic source unavailable: run `docker compose --profile sqlserver up -d --wait"
            " source-sqlserver` and `aep-sqlserver-seed`"
        )
    try:
        yield conn
    finally:
        conn.close()


def grant(admin: Any, qualified: str) -> None:
    query(admin, f"GRANT SELECT ON {qualified} TO source_reader")


@pytest.fixture
def odd_tables(admin: Any) -> Iterator[tuple[str, str]]:
    schema = "connector_" + uuid4().hex + "_库] '"
    table = 'Odd ]table%s; --中"'
    s = quote_identifier(schema)
    qualified = f"{s}.{quote_identifier(table)}"
    query(admin, f"CREATE SCHEMA {s}")
    try:
        query(
            admin,
            f"CREATE TABLE {qualified} ([id ]] '列; --] int PRIMARY KEY, payload nvarchar(max),"
            " nullable nvarchar(max), amount decimal(12, 2), created_at datetime2(0),"
            " t nvarchar(20) NOT NULL, binary_value varbinary(4), uid uniqueidentifier,"
            " flag bit, place geography, doc xml)",
        )
        query(
            admin,
            f"INSERT INTO {qualified} VALUES "
            "(1, N'{\"a\":1}', NULL, 12.50, '2026-01-01T00:00:00', N'shadow', 0x00FF,"
            " '6F9619FF-8B86-D011-B42D-00C04FC964FF', 1,"
            " geography::Point(31.2, 121.5, 4326), N'<a>1</a>'),"
            "(2, N'{\"a\":1}', N'', 25, '2026-01-02T00:00:00', N'shadow', NULL, NULL, 0,"
            " NULL, NULL),"
            "(3, NULL, N'x', NULL, '2026-01-03T00:00:00', N'shadow', NULL, NULL, NULL, NULL,"
            " NULL)",
        )
        for name in ("empty", "hidden", "column_only"):
            query(admin, f"CREATE TABLE {s}.{quote_identifier(name)} (id int, value nvarchar(max))")
        query(admin, f"CREATE VIEW {s}.a_view AS SELECT 1 AS id")
        for name in (table, "empty", "a_view"):
            grant(admin, f"{s}.{quote_identifier(name)}")
        query(admin, f"GRANT SELECT (id) ON {s}.column_only TO source_reader")
        yield schema, table
    finally:
        drop_schema(admin, schema)


def drop_schema(admin: Any, schema: str) -> None:
    s = quote_identifier(schema)
    for row in query(
        admin,
        "SELECT o.name, o.type FROM sys.objects AS o WHERE o.schema_id = SCHEMA_ID(%s)"
        " AND o.type IN ('U', 'V') ORDER BY o.type DESC",
        (schema,),
    ):
        kind = "VIEW" if row["type"].strip() == "V" else "TABLE"
        query(admin, f"DROP {kind} {s}.{quote_identifier(row['name'])}")
    query(admin, f"DROP SCHEMA {s}")


@pytest.fixture
async def mcp_url(settings: Settings) -> AsyncIterator[str]:
    """Real HTTP listener using the same application/settings as the container entrypoint."""
    app = create_server(settings).streamable_http_app(
        stateless_http=True, json_response=True, transport_security=http_security(settings)
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                        pytest.fail("MCP HTTP server stopped before startup")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{port}/mcp"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10)
