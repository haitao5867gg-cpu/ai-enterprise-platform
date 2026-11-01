from collections.abc import Callable
from typing import Any

import pytest
from pydantic import SecretStr

from aep_databricks.config import Settings
from aep_databricks.executor import Result


def settings(**changes: Any) -> Settings:
    values: dict[str, Any] = {
        "databricks_read_host": "https://adb-1.test",
        "databricks_read_token": SecretStr("token-for-tests"),
        "databricks_read_warehouse_id": "wh1",
        "databricks_read_catalogs": ["main", "prev"],
    }
    return Settings(_env_file=None, **(values | changes))  # type: ignore[call-arg]


class FakeExecutor:
    """Records each statement and answers with ``respond(statement)``."""

    def __init__(self, respond: Callable[[str], Result] | None = None) -> None:
        self.statements: list[str] = []
        self.respond = respond or (lambda statement: Result([], []))
        self.closed = False

    async def query(self, statement: str) -> Result:
        self.statements.append(statement)
        return self.respond(statement)

    async def close(self) -> None:
        self.closed = True


def columns_result(*columns: tuple[str, str], key: tuple[str, ...] = ()) -> Result:
    """What the information_schema columns query returns for a table."""
    return Result(
        [
            ("column_name", "string"),
            ("data_type", "string"),
            ("is_nullable", "string"),
            ("comment", "string"),
            ("key_position", "int"),
            ("table_comment", "string"),
        ],
        [
            [name, kind, "YES", None, key.index(name) + 1 if name in key else None, "说明"]
            for name, kind in columns
        ],
    )


@pytest.fixture
def fake() -> FakeExecutor:
    return FakeExecutor()
