"""Fixed, bounded discovery queries. No caller-supplied SQL is accepted."""

import asyncio
import json
import re
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from math import ceil
from typing import Any

import aiomysql
from aiomysql import Connection, DictCursor
from pymysql import MySQLError

from aep_mysql.config import Settings, quote_identifier, validate_identifier

TOP_VALUES_MAX = 20
TOP_VALUE_CHARS = 80


class ConnectorError(ValueError):
    """A safe error message that can be returned to an MCP caller."""


# Only direct read grants are supported. In particular FILE, EXECUTE, temporary
# tables, dynamic administrative privileges, role/proxy grants and GRANT OPTION
# are unsafe. A strict allowlist also refuses new privileges on future servers.
# Backtick-quoted column lists may themselves contain commas, ON or backticks.
_COLUMN = r"`(?:``|[^`])*`"
_READ_PRIVILEGE = rf"(?:USAGE|SELECT|SHOW VIEW)(?: \({_COLUMN}(?:, {_COLUMN})*\))?"
_READ_GRANT = re.compile(rf"GRANT {_READ_PRIVILEGE}(?:, {_READ_PRIVILEGE})* ON ")

# INFORMATION_SCHEMA lists tables with even a single column-level grant. Require
# SELECT on the whole table before exposing its metadata or issuing SELECT *.
_TABLE_ACCESS = """
    t.TABLE_TYPE = 'BASE TABLE'
    AND t.TABLE_SCHEMA NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
    AND (
        EXISTS (SELECT 1 FROM information_schema.USER_PRIVILEGES AS p
                WHERE p.GRANTEE = %s AND p.PRIVILEGE_TYPE = 'SELECT')
        OR EXISTS (SELECT 1 FROM information_schema.SCHEMA_PRIVILEGES AS p
                   WHERE p.GRANTEE = %s AND p.PRIVILEGE_TYPE = 'SELECT'
                     AND p.TABLE_SCHEMA = t.TABLE_SCHEMA)
        OR EXISTS (SELECT 1 FROM information_schema.TABLE_PRIVILEGES AS p
                   WHERE p.GRANTEE = %s AND p.PRIVILEGE_TYPE = 'SELECT'
                     AND p.TABLE_SCHEMA = t.TABLE_SCHEMA AND p.TABLE_NAME = t.TABLE_NAME)
    )
"""


def _read_only_grant(grant: str) -> bool:
    return bool(_READ_GRANT.match(grant)) and not grant.endswith(" WITH GRANT OPTION")


def _json_value(value: Any, data_type: str) -> Any:
    if value is None:
        return None
    if data_type == "json":
        return json.loads(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, bytes):
        return "\\x" + value.hex()
    return value


def _value_text(value: Any, data_type: str) -> str:
    if data_type == "json":
        return json.dumps(
            json.loads(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    if isinstance(value, Decimal):
        return str(value)
    return str(_json_value(value, data_type))


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def _query(
        self, conn: Connection, query: str, params: Sequence[Any] = ()
    ) -> list[dict[str, Any]]:
        # max_execution_time covers SELECT on the server. This client deadline
        # additionally bounds metadata, lock waits and functions such as SLEEP.
        async with asyncio.timeout(self.settings.mysql_query_timeout_ms / 1000):
            async with conn.cursor(DictCursor) as cursor:
                await cursor.execute(query, params or None)
                return list(await cursor.fetchall())

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[Connection]:
        """Apply timeouts, read-only mode and privilege checks on EVERY session."""
        settings = self.settings
        conn: Connection | None = None
        try:
            conn = await aiomysql.connect(
                host=settings.mysql_host,
                port=settings.mysql_port,
                db=settings.mysql_database,
                user=settings.mysql_user,
                password=settings.mysql_password.get_secret_value(),
                connect_timeout=settings.mysql_connect_timeout_seconds,
                charset="utf8mb4",
                autocommit=True,
                local_infile=False,
            )
            await self._query(conn, "SET SESSION TRANSACTION READ ONLY")
            await self._query(
                conn, "SET SESSION max_execution_time = %s", (settings.mysql_query_timeout_ms,)
            )
            await self._query(
                conn,
                "SET SESSION lock_wait_timeout = %s",
                (ceil(settings.mysql_query_timeout_ms / 1000),),
            )
            await self._query(conn, "SET SESSION time_zone = '+00:00'")
            [roles] = await self._query(conn, "SELECT @@GLOBAL.mandatory_roles AS mandatory_roles")
            grants = await self._query(conn, "SHOW GRANTS FOR CURRENT_USER")
            if (
                roles["mandatory_roles"]
                or not grants
                or any(not _read_only_grant(str(next(iter(row.values())))) for row in grants)
            ):
                raise ConnectorError(
                    "Configure an account with direct SELECT-only privileges and no roles"
                )
            # Keep every discovery operation in one read-only transaction so
            # its metadata and bounded sample share the same session settings.
            await self._query(conn, "START TRANSACTION READ ONLY")
            yield conn
        except TimeoutError:
            raise ConnectorError("MySQL query exceeded the configured timeout") from None
        except MySQLError as exc:
            if exc.args and exc.args[0] in (1205, 1317, 3024):
                raise ConnectorError("MySQL query exceeded the configured timeout") from None
            # Driver exceptions can contain hostnames, SQL and source data.
            raise ConnectorError("MySQL request failed; check source access and settings") from None
        finally:
            if conn is not None:
                conn.close()

    async def _grantee(self, conn: Connection) -> str:
        [row] = await self._query(conn, "SELECT CURRENT_USER() AS account")
        user, host = row["account"].rsplit("@", 1)
        return f"'{user}'@'{host}'"

    async def list_schemas(self) -> dict[str, list[str]]:
        async with self.connect() as conn:
            rows = await self._query(
                conn,
                """
                SELECT SCHEMA_NAME AS name FROM information_schema.SCHEMATA
                WHERE SCHEMA_NAME NOT IN
                    ('information_schema', 'mysql', 'performance_schema', 'sys')
                ORDER BY SCHEMA_NAME
                """,
            )
        return {"schemas": [row["name"] for row in rows]}

    async def list_tables(self, schema: str) -> dict[str, Any]:
        validate_identifier(schema)
        async with self.connect() as conn:
            grantee = await self._grantee(conn)
            rows = await self._query(
                conn,
                "SELECT t.TABLE_NAME AS name FROM information_schema.TABLES AS t "
                "WHERE t.TABLE_SCHEMA = %s AND " + _TABLE_ACCESS + " ORDER BY t.TABLE_NAME",
                (schema, grantee, grantee, grantee),
            )
        return {"schema": schema, "tables": [row["name"] for row in rows]}

    async def _table(self, conn: Connection, schema: str, table: str) -> dict[str, Any]:
        validate_identifier(schema)
        validate_identifier(table)
        grantee = await self._grantee(conn)
        rows = await self._query(
            conn,
            "SELECT t.TABLE_ROWS AS estimated_row_count FROM information_schema.TABLES AS t "
            "WHERE t.TABLE_SCHEMA = %s AND t.TABLE_NAME = %s AND " + _TABLE_ACCESS,
            (schema, table, grantee, grantee, grantee),
        )
        if not rows:
            raise ConnectorError("Table does not exist or SELECT access is not granted")
        return rows[0]

    async def _columns(self, conn: Connection, schema: str, table: str) -> list[dict[str, Any]]:
        rows = await self._query(
            conn,
            """
            SELECT c.COLUMN_NAME AS name, c.COLUMN_TYPE AS data_type,
                   c.IS_NULLABLE = 'YES' AS nullable,
                   EXISTS (
                       SELECT 1 FROM information_schema.KEY_COLUMN_USAGE AS k
                       JOIN information_schema.TABLE_CONSTRAINTS AS p
                         ON p.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA
                        AND p.TABLE_NAME = k.TABLE_NAME AND p.CONSTRAINT_NAME = k.CONSTRAINT_NAME
                       WHERE k.TABLE_SCHEMA = c.TABLE_SCHEMA AND k.TABLE_NAME = c.TABLE_NAME
                         AND k.COLUMN_NAME = c.COLUMN_NAME AND p.CONSTRAINT_TYPE = 'PRIMARY KEY'
                   ) AS primary_key
            FROM information_schema.COLUMNS AS c
            WHERE c.TABLE_SCHEMA = %s AND c.TABLE_NAME = %s ORDER BY c.ORDINAL_POSITION
            """,
            (schema, table),
        )
        return [
            {**row, "nullable": bool(row["nullable"]), "primary_key": bool(row["primary_key"])}
            for row in rows
        ]

    async def describe_table(self, schema: str, table: str) -> dict[str, Any]:
        async with self.connect() as conn:
            await self._table(conn, schema, table)
            columns = await self._columns(conn, schema, table)
            keys = await self._query(
                conn,
                """
                SELECT CONSTRAINT_NAME AS name, COLUMN_NAME AS source_column,
                       REFERENCED_TABLE_SCHEMA AS referenced_schema,
                       REFERENCED_TABLE_NAME AS referenced_table,
                       REFERENCED_COLUMN_NAME AS target_column
                FROM information_schema.KEY_COLUMN_USAGE
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND REFERENCED_TABLE_NAME IS NOT NULL
                ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION
                """,
                (schema, table),
            )
        foreign_keys: dict[str, dict[str, Any]] = {}
        for key in keys:
            foreign_key = foreign_keys.setdefault(
                key["name"],
                {
                    "name": key["name"],
                    "columns": [],
                    "referenced_schema": key["referenced_schema"],
                    "referenced_table": key["referenced_table"],
                    "referenced_columns": [],
                },
            )
            foreign_key["columns"].append(key["source_column"])
            foreign_key["referenced_columns"].append(key["target_column"])
        return {
            "schema": schema,
            "table": table,
            "columns": columns,
            "foreign_keys": list(foreign_keys.values()),
        }

    async def _sample(
        self, conn: Connection, schema: str, table: str, limit: int
    ) -> list[dict[str, Any]]:
        qualified = f"{quote_identifier(schema)}.{quote_identifier(table)}"
        # PyMySQL applies %-formatting when binding parameters. Literal percent
        # signs inside quoted identifiers must survive that formatting as well.
        qualified = qualified.replace("%", "%%")
        return await self._query(conn, f"SELECT * FROM {qualified} LIMIT %s", (limit,))

    async def sample_rows(self, schema: str, table: str, limit: int = 10) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ConnectorError("limit must be an integer between 1 and 100")
        async with self.connect() as conn:
            await self._table(conn, schema, table)
            columns = await self._columns(conn, schema, table)
            sample = await self._sample(conn, schema, table, limit)
        rows = [
            {
                column["name"]: _json_value(row[column["name"]], column["data_type"])
                for column in columns
            }
            for row in sample
        ]
        return {"schema": schema, "table": table, "rows": rows}

    async def table_stats(self, schema: str, table: str) -> dict[str, Any]:
        async with self.connect() as conn:
            relation = await self._table(conn, schema, table)
            columns = await self._columns(conn, schema, table)
            sample = await self._sample(conn, schema, table, self.settings.mysql_stats_sample_size)
        stats = []
        for column in columns:
            name = column["name"]
            # Count complete values from the SAME bounded sample. Counting here
            # avoids MySQL collations merging case or trailing-space variants,
            # GROUP BY truncation and JSON/BLOB equality limitations.
            values = Counter(
                _value_text(row[name], column["data_type"])
                for row in sample
                if row[name] is not None
            )
            non_null = sum(values.values())
            stat = {
                "name": name,
                "null_fraction": (len(sample) - non_null) / len(sample) if sample else None,
                "distinct_count": len(values),
            }
            if 0 < len(values) <= TOP_VALUES_MAX:
                stat["top_values"] = [
                    {"value": value[:TOP_VALUE_CHARS], "count": count}
                    | ({"truncated": True} if len(value) > TOP_VALUE_CHARS else {})
                    for value, count in sorted(values.items(), key=lambda item: (-item[1], item[0]))
                ]
            stats.append(stat)
        return {
            "schema": schema,
            "table": table,
            "estimated_row_count": relation["estimated_row_count"],
            "sample_limit": self.settings.mysql_stats_sample_size,
            "sample_row_count": len(sample),
            "columns": stats,
        }
