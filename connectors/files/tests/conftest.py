import asyncio
import json
import socket
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import pytest
import uvicorn

from aep_files.config import Settings
from aep_files.database import Database
from aep_files.server import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(uploads_dir=tmp_path)


@pytest.fixture
def database(settings: Settings) -> Database:
    return Database(settings)


@pytest.fixture
def dataset(settings: Settings) -> Callable[..., tuple[str, str, dict[str, Any]]]:
    def write(query: str | None = None, table: str = "orders") -> tuple[str, str, dict[str, Any]]:
        dataset_id = str(uuid4())
        schema = "upload_" + dataset_id[:8]
        directory = settings.uploads_dir / dataset_id
        directory.mkdir()
        with duckdb.connect() as conn:
            relation = conn.sql(
                query
                or """
                SELECT * FROM (VALUES
                    (1, 'paid', 'x', DATE '2026-10-01'),
                    (2, 'paid', NULL, DATE '2026-10-02'),
                    (3, 'open', 'x', DATE '2026-10-03'),
                    (4, NULL, 'y', NULL)
                ) AS t(id, status, nullable, ordered_at)
            """
            )
            relation.write_parquet(str(directory / "source.parquet"))
            columns = [
                {"name": name, "data_type": str(dtype), "nullable": True, "primary_key": False}
                for name, dtype in zip(relation.columns, relation.types, strict=True)
            ]
            count = relation.count("*").fetchone()[0]
        manifest = {
            "dataset_id": dataset_id,
            "schema": schema,
            "tables": [
                {
                    "table": table,
                    "file": "source.parquet",
                    "row_count": count,
                    "columns": columns,
                }
            ],
        }
        (directory / "manifest.json").write_text(json.dumps(manifest))
        return dataset_id, schema, manifest

    return write


@pytest.fixture
async def mcp_base(settings: Settings) -> AsyncIterator[str]:
    server = uvicorn.Server(uvicorn.Config(create_app(settings), log_level="warning"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        await task
                        pytest.fail("MCP server exited before startup")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=10)
