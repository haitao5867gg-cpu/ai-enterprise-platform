import asyncio
import socket
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest
import uvicorn
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from aep_postgres.config import Settings
from aep_postgres.database import Database
from aep_postgres.server import create_server, http_security


class SeedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEP_SOURCE_", env_file=".env", extra="ignore")
    postgres_admin_password: SecretStr = SecretStr("local-source-admin")


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def database(settings: Settings) -> Database:
    return Database(settings)


@pytest.fixture
async def admin(settings: Settings) -> AsyncIterator[AsyncConnection[dict[str, Any]]]:
    """Only the synthetic compose database is modified by these integration fixtures."""
    try:
        conn = await psycopg.AsyncConnection.connect(
            host=settings.postgres_host,
            port=settings.postgres_port,
            dbname=settings.postgres_database,
            user="source_admin",
            password=SeedSettings().postgres_admin_password.get_secret_value(),
            autocommit=True,
            row_factory=dict_row,
            connect_timeout=5,
        )
    except psycopg.Error:
        pytest.fail(
            "Synthetic source unavailable: run `docker compose up -d --wait source-postgres`"
        )
    async with conn:
        yield conn


@pytest.fixture
async def odd_tables(admin: AsyncConnection[dict[str, Any]]) -> AsyncIterator[tuple[str, str]]:
    schema = "connector_test_" + uuid4().hex
    table = 'Odd "table; --'
    qualified = sql.Identifier(schema, table)
    await admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        await admin.execute(
            sql.SQL("CREATE DOMAIN {} AS text NOT NULL").format(
                sql.Identifier(schema, "required_text")
            )
        )
        await admin.execute(
            sql.SQL(
                'CREATE TABLE {} ("id \'column" int PRIMARY KEY, payload json, '
                "nullable text, amount numeric(12, 2), created_at timestamptz, t {})"
            ).format(qualified, sql.Identifier(schema, "required_text"))
        )
        await admin.execute(
            sql.SQL(
                "INSERT INTO {} VALUES "
                "(1, '{{\"a\":1}}', NULL, 12.50, '2026-01-01T00:00:00Z', 'shadow'), "
                "(2, '{{\"a\":1}}', '', 25, '2026-01-02T00:00:00Z', 'shadow'), "
                "(3, NULL, 'x', NULL, '2026-01-03T00:00:00Z', 'shadow')"
            ).format(qualified)
        )
        for name in ("empty", "hidden"):
            await admin.execute(
                sql.SQL("CREATE TABLE {} (id int, value text)").format(sql.Identifier(schema, name))
            )
        await admin.execute(
            sql.SQL("CREATE VIEW {} AS SELECT 1 AS id").format(sql.Identifier(schema, "a_view"))
        )
        await admin.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO source_reader").format(sql.Identifier(schema))
        )
        await admin.execute(
            sql.SQL("GRANT SELECT ON {}, {} TO source_reader").format(
                qualified, sql.Identifier(schema, "empty")
            )
        )
        await admin.execute(sql.SQL("ANALYZE {}").format(qualified))
        await admin.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(schema, "empty")))
        yield schema, table
    finally:
        await admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


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
