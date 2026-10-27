import asyncio
import socket
from collections.abc import AsyncIterator, Sequence
from typing import Any
from uuid import uuid4

import aiomysql
import pytest
import uvicorn
from aiomysql import Connection, DictCursor
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from pymysql import MySQLError

from aep_mysql.config import Settings, quote_identifier
from aep_mysql.database import Database
from aep_mysql.server import create_server, http_security


class SeedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEP_SOURCE_", env_file=".env", extra="ignore")
    mysql_admin_password: SecretStr = SecretStr("local-source-admin")


async def query(conn: Connection, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    async with conn.cursor(DictCursor) as cursor:
        await cursor.execute(sql, params or None)
        return list(await cursor.fetchall())


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def database(settings: Settings) -> Database:
    return Database(settings)


@pytest.fixture
async def admin(settings: Settings) -> AsyncIterator[Connection]:
    """Only the synthetic compose database is modified by these integration fixtures."""
    try:
        conn = await aiomysql.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            db=settings.mysql_database,
            user="root",
            password=SeedSettings().mysql_admin_password.get_secret_value(),
            autocommit=True,
            charset="utf8mb4",
            connect_timeout=5,
        )
    except MySQLError:
        pytest.fail("Synthetic source unavailable: run `docker compose up -d --wait source-mysql`")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
async def odd_tables(admin: Connection) -> AsyncIterator[tuple[str, str]]:
    schema = "connector_" + uuid4().hex + "_库` '"
    table = 'Odd `table%s; --中"'
    qualified = f"{quote_identifier(schema)}.{quote_identifier(table)}"
    await query(admin, f"CREATE DATABASE {quote_identifier(schema)}")
    try:
        await query(
            admin,
            f"CREATE TABLE {qualified} (`id `` '列; --` int PRIMARY KEY, payload json, "
            "nullable text, amount decimal(12, 2), created_at datetime, t text NOT NULL, "
            "binary_value varbinary(4))",
        )
        await query(
            admin,
            f"INSERT INTO {qualified} VALUES "
            "(1, '{\"a\":1}', NULL, 12.50, '2026-01-01 00:00:00', 'shadow', X'00FF'), "
            "(2, '{\"a\":1}', '', 25, '2026-01-02 00:00:00', 'shadow', NULL), "
            "(3, NULL, 'x', NULL, '2026-01-03 00:00:00', 'shadow', NULL)",
        )
        for name in ("empty", "hidden", "column_only"):
            await query(
                admin,
                f"CREATE TABLE {quote_identifier(schema)}.{quote_identifier(name)} "
                "(id int, value text)",
            )
        await query(admin, f"CREATE VIEW {quote_identifier(schema)}.a_view AS SELECT 1 AS id")
        for name in (table, "empty", "a_view"):
            await query(
                admin,
                f"GRANT SELECT ON {quote_identifier(schema)}.{quote_identifier(name)} "
                "TO 'source_reader'@'%'",
            )
        await query(
            admin,
            f"GRANT SELECT (id) ON {quote_identifier(schema)}.column_only TO 'source_reader'@'%'",
        )
        await query(admin, f"ANALYZE TABLE {qualified}")
        yield schema, table
    finally:
        await query(admin, f"DROP DATABASE {quote_identifier(schema)}")


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
