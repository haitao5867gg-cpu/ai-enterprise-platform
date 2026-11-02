"""Fixed, bounded discovery queries. No caller-supplied SQL is accepted.

pymssql is synchronous; every operation opens its own connection in a worker thread.
"""

import asyncio
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, TypeVar
from uuid import UUID

import pymssql

from aep_sqlserver.config import Settings, quote_identifier, validate_identifier

TOP_VALUES_MAX = 20
TOP_VALUE_CHARS = 80
T = TypeVar("T")


class ConnectorError(ValueError):
    """A safe error message that can be returned to an MCP caller."""


# What a read-only account may hold. Everything else (writes, EXECUTE, ALTER, CONTROL,
# IMPERSONATE, CREATE ..., server roles) is refused; a strict allowlist also refuses
# permissions that future versions add.
SERVER_ALLOWED = frozenset({"CONNECT SQL", "VIEW ANY DATABASE"})
DATABASE_ALLOWED = frozenset(
    {
        "CONNECT",
        "SELECT",
        "VIEW DEFINITION",
        "SHOWPLAN",
        "VIEW DATABASE STATE",
        "VIEW DATABASE PERFORMANCE STATE",
        # The public role holds these two in every database.
        "VIEW ANY COLUMN ENCRYPTION KEY DEFINITION",
        "VIEW ANY COLUMN MASTER KEY DEFINITION",
    }
)
OBJECT_ALLOWED = frozenset({"SELECT", "VIEW DEFINITION", "REFERENCES", "VIEW CHANGE TRACKING"})

# Grants on schemas, objects and principals to the account or any role it is in, other
# than public's grants on system views. Ownership is not a grant: owning a schema or a
# role is checked separately.
_GRANTS = """
SELECT DISTINCT p.permission_name AS name FROM sys.database_permissions AS p
WHERE p.state IN ('G', 'W') AND p.class <> 0
  AND (p.grantee_principal_id = DATABASE_PRINCIPAL_ID()
       OR IS_ROLEMEMBER(USER_NAME(p.grantee_principal_id)) = 1)
  AND NOT (p.class = 1 AND OBJECT_SCHEMA_NAME(p.major_id) IN ('sys', 'INFORMATION_SCHEMA'))
"""
# Owning a schema means CONTROL over it. The fixed roles' own empty schemas (db_owner,
# db_datareader, ...; ids 16384-16399) do not count: their roles are checked above.
_OWNS = """
SELECT count(*) AS n FROM sys.schemas AS s
WHERE (s.principal_id = DATABASE_PRINCIPAL_ID()
       OR IS_ROLEMEMBER(USER_NAME(s.principal_id)) = 1)
  AND s.schema_id NOT BETWEEN 16384 AND 16399
"""
# Whole-table SELECT is required: a column-only grant does not count.
_READABLE = (
    "t.is_ms_shipped = 0 AND HAS_PERMS_BY_NAME("
    "QUOTENAME(s.name) + '.' + QUOTENAME(t.name), 'OBJECT', 'SELECT') = 1"
)
SYSTEM_SCHEMAS = ("sys", "INFORMATION_SCHEMA", "guest")
# Types that cannot appear in ORDER BY, and CLR types the driver cannot read directly.
UNORDERABLE = frozenset({"text", "ntext", "image", "xml", "geography", "geometry"})
AS_TEXT = frozenset({"geography", "geometry", "hierarchyid"})
SIZED = frozenset({"char", "varchar", "binary", "varbinary"})
UNICODE = frozenset({"nchar", "nvarchar"})
SCALED = frozenset({"datetime2", "time", "datetimeoffset"})


def data_type(row: dict[str, Any]) -> str:
    """sys.columns type details as SQL Server writes them: nvarchar(50), decimal(12,2)."""
    name, length = row["type_name"], row["max_length"]
    if name in SIZED or name in UNICODE:
        if length == -1:
            return f"{name}(max)"
        return f"{name}({length // 2 if name in UNICODE else length})"
    if name in ("decimal", "numeric"):
        return f"{name}({row['precision']},{row['scale']})"
    if name in SCALED:
        return f"{name}({row['scale']})"
    return name


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return "\\x" + bytes(value).hex()
    return str(value)


def _value_text(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    return str(_json_value(value))


def _projection(columns: list[dict[str, Any]]) -> str:
    parts = []
    for column in columns:
        quoted = quote_identifier(column["name"])
        if column["type_name"] in AS_TEXT:
            parts.append(f"{quoted}.ToString() AS {quoted}")
        elif column["type_name"] == "sql_variant":
            parts.append(f"CAST({quoted} AS nvarchar(4000)) AS {quoted}")
        else:
            parts.append(quoted)
    return ", ".join(parts)


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _query(conn: Any, query: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        cursor = conn.cursor(as_dict=True)
        try:
            # pymssql %-formats the query whenever a parameter argument is passed, even None
            # (which it binds as NULL): pass none without parameters.
            if params:
                cursor.execute(query, tuple(params))
            else:
                cursor.execute(query)
            return list(cursor.fetchall()) if cursor.description else []
        finally:
            cursor.close()

    def _check_read_only(self, conn: Any) -> None:
        server = {
            row["name"]
            for row in self._query(
                conn, "SELECT permission_name AS name FROM fn_my_permissions(NULL, 'SERVER')"
            )
        }
        database = {
            row["name"]
            for row in self._query(
                conn, "SELECT permission_name AS name FROM fn_my_permissions(NULL, 'DATABASE')"
            )
        }
        grants = {row["name"] for row in self._query(conn, _GRANTS)}
        [owns] = self._query(conn, _OWNS)
        if (
            server - SERVER_ALLOWED
            or database - DATABASE_ALLOWED
            or grants - OBJECT_ALLOWED
            or owns["n"]
        ):
            raise ConnectorError(
                "Configure a login whose database user may only SELECT "
                "(no write, EXECUTE, ALTER or CONTROL permissions, roles or owned schemas)"
            )

    @contextmanager
    def connect(self) -> Iterator[Any]:
        """Apply timeouts and privilege checks on EVERY connection, and roll back at the end."""
        settings = self.settings
        conn: Any = None
        try:
            conn = pymssql.connect(
                server=settings.sqlserver_host,
                port=str(settings.sqlserver_port),
                user=settings.sqlserver_user,
                password=settings.sqlserver_password.get_secret_value(),
                database=settings.sqlserver_database,
                login_timeout=settings.sqlserver_connect_timeout_seconds,
                timeout=settings.sqlserver_query_timeout_seconds,
                appname="aep-sqlserver",
                autocommit=False,
            )
            lock_ms = settings.sqlserver_query_timeout_seconds * 1000
            self._query(conn, f"SET LOCK_TIMEOUT {lock_ms}")
            self._query(conn, "SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            self._check_read_only(conn)
            yield conn
        except pymssql.Error as exc:
            text = str(exc)
            if "timed out" in text or "1222" in text:
                raise ConnectorError("SQL Server query exceeded the configured timeout") from None
            # Driver exceptions can contain hostnames, SQL and source data.
            raise ConnectorError(
                "SQL Server request failed; check source access and settings"
            ) from None
        finally:
            if conn is not None:
                # Every operation only reads; nothing it did may persist.
                # After a timeout the driver has already dropped the connection.
                for finish in (conn.rollback, conn.close):
                    try:
                        finish()
                    except Exception:  # noqa: BLE001 - cleanup must not hide the real error
                        pass

    async def _run(self, operation: Callable[[Any], T]) -> T:
        def work() -> T:
            with self.connect() as conn:
                return operation(conn)

        return await asyncio.to_thread(work)

    async def check(self) -> None:
        await self._run(lambda _conn: None)

    async def list_schemas(self) -> dict[str, list[str]]:
        def operation(conn: Any) -> list[dict[str, Any]]:
            return self._query(
                conn,
                "SELECT s.name FROM sys.schemas AS s WHERE s.name NOT IN (%s, %s, %s) "
                "AND EXISTS (SELECT 1 FROM sys.tables AS t WHERE t.schema_id = s.schema_id AND "
                + _READABLE
                + ") ORDER BY s.name",
                SYSTEM_SCHEMAS,
            )

        return {"schemas": [row["name"] for row in await self._run(operation)]}

    async def list_tables(self, schema: str) -> dict[str, Any]:
        validate_identifier(schema)

        def operation(conn: Any) -> list[dict[str, Any]]:
            return self._query(
                conn,
                "SELECT t.name FROM sys.tables AS t JOIN sys.schemas AS s "
                "ON s.schema_id = t.schema_id WHERE s.name = %s AND " + _READABLE + " "
                "ORDER BY t.name",
                (schema,),
            )

        return {"schema": schema, "tables": [row["name"] for row in await self._run(operation)]}

    def _table(self, conn: Any, schema: str, table: str) -> dict[str, Any]:
        validate_identifier(schema)
        validate_identifier(table)
        rows = self._query(
            conn,
            "SELECT t.object_id, (SELECT SUM(p.rows) FROM sys.partitions AS p "
            "WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)) AS estimated_row_count "
            "FROM sys.tables AS t JOIN sys.schemas AS s ON s.schema_id = t.schema_id "
            "WHERE s.name = %s AND t.name = %s AND " + _READABLE,
            (schema, table),
        )
        if not rows:
            raise ConnectorError("Table does not exist or SELECT access is not granted")
        return rows[0]

    def _columns(self, conn: Any, object_id: int) -> list[dict[str, Any]]:
        rows = self._query(
            conn,
            """
            SELECT c.name,
                   -- CLR types (geography, geometry, hierarchyid) share system type 240.
                   TYPE_NAME(CASE WHEN c.system_type_id = 240 THEN c.user_type_id
                                  ELSE c.system_type_id END) AS type_name,
                   c.max_length, c.precision, c.scale, c.is_nullable,
                   (SELECT ic.key_ordinal FROM sys.indexes AS i JOIN sys.index_columns AS ic
                      ON ic.object_id = i.object_id AND ic.index_id = i.index_id
                    WHERE i.object_id = c.object_id AND i.is_primary_key = 1
                      AND ic.column_id = c.column_id) AS key_ordinal
            FROM sys.columns AS c WHERE c.object_id = %d ORDER BY c.column_id
            """,
            (object_id,),
        )
        return [
            {
                "name": row["name"],
                "type_name": row["type_name"],
                "data_type": data_type(row),
                "nullable": bool(row["is_nullable"]),
                "primary_key": row["key_ordinal"] is not None,
                "key_ordinal": row["key_ordinal"],
            }
            for row in rows
        ]

    @staticmethod
    def _public(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {key: column[key] for key in ("name", "data_type", "nullable", "primary_key")}
            for column in columns
        ]

    async def describe_table(self, schema: str, table: str) -> dict[str, Any]:
        def operation(conn: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            relation = self._table(conn, schema, table)
            columns = self._columns(conn, relation["object_id"])
            keys = self._query(
                conn,
                """
                SELECT fk.name, pc.name AS source_column,
                       OBJECT_SCHEMA_NAME(fk.referenced_object_id) AS referenced_schema,
                       OBJECT_NAME(fk.referenced_object_id) AS referenced_table,
                       rc.name AS target_column
                FROM sys.foreign_keys AS fk
                JOIN sys.foreign_key_columns AS fkc ON fkc.constraint_object_id = fk.object_id
                JOIN sys.columns AS pc
                  ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
                JOIN sys.columns AS rc ON rc.object_id = fkc.referenced_object_id
                 AND rc.column_id = fkc.referenced_column_id
                WHERE fk.parent_object_id = %d
                ORDER BY fk.name, fkc.constraint_column_id
                """,
                (relation["object_id"],),
            )
            return columns, keys

        columns, keys = await self._run(operation)
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
            "columns": self._public(columns),
            "foreign_keys": list(foreign_keys.values()),
        }

    def _sample(
        self, conn: Any, schema: str, table: str, columns: list[dict[str, Any]], limit: int
    ) -> list[dict[str, Any]]:
        qualified = f"{quote_identifier(schema)}.{quote_identifier(table)}"
        # No bound parameters: pymssql's placeholder parser would read a % in a name.
        # limit is an int checked by every caller.
        return self._query(
            conn, f"SELECT TOP ({int(limit)}) {_projection(columns)} FROM {qualified}"
        )

    async def sample_rows(self, schema: str, table: str, limit: int = 10) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ConnectorError("limit must be an integer between 1 and 100")

        def operation(conn: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            relation = self._table(conn, schema, table)
            columns = self._columns(conn, relation["object_id"])
            return columns, self._sample(conn, schema, table, columns, limit)

        columns, sample = await self._run(operation)
        rows = [{c["name"]: _json_value(row[c["name"]]) for c in columns} for row in sample]
        return {"schema": schema, "table": table, "rows": rows}

    async def read_rows(
        self, schema: str, table: str, offset: int = 0, limit: int = 1000
    ) -> dict[str, Any]:
        if type(offset) is not int or offset < 0:
            raise ConnectorError("offset must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ConnectorError("limit must be an integer between 1 and 5000")

        def operation(conn: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            relation = self._table(conn, schema, table)
            columns = self._columns(conn, relation["object_id"])
            keys = sorted((c for c in columns if c["primary_key"]), key=lambda c: c["key_ordinal"])
            # Without a key, order by every column that can be ordered.
            order = keys or [c for c in columns if c["type_name"] not in UNORDERABLE]
            ordering = (
                ", ".join(quote_identifier(c["name"]) for c in order) if order else "(SELECT NULL)"
            )
            qualified = f"{quote_identifier(schema)}.{quote_identifier(table)}"
            select = f"SELECT {_projection(columns)} FROM {qualified} ORDER BY {ordering}"
            # As in _sample, no bound parameters; offset and limit are checked ints.
            page = self._query(
                conn, f"{select} OFFSET {int(offset)} ROWS FETCH NEXT {int(limit) + 1} ROWS ONLY"
            )
            return columns, page

        columns, page = await self._run(operation)
        return {
            "schema": schema,
            "table": table,
            "columns": [c["name"] for c in columns],
            "rows": [
                {c["name"]: _json_value(row[c["name"]]) for c in columns} for row in page[:limit]
            ],
            "next_offset": offset + limit if len(page) > limit else None,
        }

    async def table_stats(self, schema: str, table: str) -> dict[str, Any]:
        size = self.settings.sqlserver_stats_sample_size

        def operation(
            conn: Any,
        ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
            relation = self._table(conn, schema, table)
            columns = self._columns(conn, relation["object_id"])
            return relation, columns, self._sample(conn, schema, table, columns, size)

        relation, columns, sample = await self._run(operation)
        stats = []
        for column in columns:
            name = column["name"]
            # Count complete values from the SAME bounded sample, in Python, so collations
            # do not merge case or trailing-space variants.
            values = Counter(_value_text(row[name]) for row in sample if row[name] is not None)
            non_null = sum(values.values())
            stat: dict[str, Any] = {
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
        estimate = relation["estimated_row_count"]
        return {
            "schema": schema,
            "table": table,
            "estimated_row_count": int(estimate) if estimate is not None else None,
            "sample_limit": size,
            "sample_row_count": len(sample),
            "columns": stats,
        }
