"""Fixed, bounded discovery queries. No caller-supplied SQL is accepted."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from aep_postgres.config import Settings, validate_identifier

# Low-cardinality columns (at most this many distinct values in the sample) also get
# their value counts, so a caller can see the real spellings of statuses and codes.
TOP_VALUES_MAX = 20
TOP_VALUE_CHARS = 80


class ConnectorError(ValueError):
    """A safe error message that can be returned to an MCP caller."""


# Check effective privileges as well as ownership and roles the login can assume.
# This refuses writable/owner accounts even though each connection is also read-only.
_UNSAFE_ROLE = """
WITH roles AS (
    SELECT * FROM pg_catalog.pg_roles
    WHERE pg_catalog.pg_has_role(current_user, oid, 'MEMBER')
)
SELECT
    EXISTS (
        SELECT 1 FROM roles
        WHERE rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
           OR rolname IN ('pg_write_server_files', 'pg_execute_server_program',
                          'pg_signal_backend', 'pg_checkpoint', 'pg_maintain',
                          'pg_create_subscription')
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_database AS d CROSS JOIN roles AS r
        WHERE datname = current_database()
          AND (pg_catalog.pg_has_role(current_user, datdba, 'MEMBER')
               OR pg_catalog.has_database_privilege(r.oid, d.oid, 'CREATE,TEMP'))
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_namespace AS n CROSS JOIN roles AS r
        WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
          AND (pg_catalog.pg_has_role(current_user, nspowner, 'MEMBER')
               OR pg_catalog.has_schema_privilege(r.oid, n.oid, 'CREATE'))
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        CROSS JOIN roles AS r
        WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
          AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
          AND (pg_catalog.pg_has_role(current_user, c.relowner, 'MEMBER')
               OR pg_catalog.has_table_privilege(
                   r.oid, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
               OR pg_catalog.has_any_column_privilege(r.oid, c.oid, 'INSERT,UPDATE,REFERENCES')
               OR CASE WHEN current_setting('server_version_num')::int >= 170000
                       THEN pg_catalog.has_table_privilege(r.oid, c.oid, 'MAINTAIN')
                       ELSE false END)
    ) OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_class AS c CROSS JOIN roles AS r
        WHERE c.relkind = 'S'
          AND (pg_catalog.pg_has_role(current_user, c.relowner, 'MEMBER')
               OR pg_catalog.has_sequence_privilege(r.oid, c.oid, 'USAGE,UPDATE'))
    ) AS unsafe
"""


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection[dict[str, Any]]]:
        """Apply timeouts and read-only mode before the first query on EVERY session."""
        settings = self.settings
        timeout = settings.postgres_query_timeout_ms
        try:
            async with await AsyncConnection.connect(
                host=settings.postgres_host,
                port=settings.postgres_port,
                dbname=settings.postgres_database,
                user=settings.postgres_user,
                password=settings.postgres_password.get_secret_value(),
                connect_timeout=settings.postgres_connect_timeout_seconds,
                options=(
                    "-c default_transaction_read_only=on "
                    f"-c statement_timeout={timeout} -c lock_timeout={timeout} "
                    "-c idle_in_transaction_session_timeout=10000 -c search_path=pg_catalog"
                ),
                row_factory=dict_row,
            ) as conn:
                await conn.set_read_only(True)
                row = await (await conn.execute(_UNSAFE_ROLE)).fetchone()
                if row is None or row["unsafe"]:
                    raise ConnectorError(
                        "Configure a non-owner account with SELECT-only privileges"
                    )
                yield conn
        except psycopg.errors.QueryCanceled:
            raise ConnectorError("PostgreSQL query exceeded the configured timeout") from None
        except psycopg.Error:
            # Driver exceptions can contain hostnames, SQL and source data.
            raise ConnectorError(
                "PostgreSQL request failed; check source access and settings"
            ) from None

    async def list_schemas(self) -> dict[str, list[str]]:
        async with self.connect() as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT nspname AS name FROM pg_catalog.pg_namespace
                    WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
                      AND pg_catalog.has_schema_privilege(oid, 'USAGE')
                    ORDER BY nspname
                    """
                )
            ).fetchall()
        return {"schemas": [row["name"] for row in rows]}

    async def list_tables(self, schema: str) -> dict[str, Any]:
        validate_identifier(schema)
        async with self.connect() as conn:
            rows = await (
                await conn.execute(
                    """
                    SELECT c.relname AS name FROM pg_catalog.pg_class AS c
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                    WHERE n.nspname = %s AND n.nspname !~ '^pg_'
                      AND n.nspname <> 'information_schema' AND c.relkind IN ('r', 'p')
                      AND pg_catalog.has_schema_privilege(n.oid, 'USAGE')
                      AND pg_catalog.has_table_privilege(c.oid, 'SELECT')
                    ORDER BY c.relname
                    """,
                    (schema,),
                )
            ).fetchall()
        return {"schema": schema, "tables": [row["name"] for row in rows]}

    async def _table(
        self, conn: AsyncConnection[dict[str, Any]], schema: str, table: str
    ) -> dict[str, Any]:
        validate_identifier(schema)
        validate_identifier(table)
        row = await (
            await conn.execute(
                """
                SELECT c.oid, CASE WHEN c.reltuples < 0 THEN NULL
                                  ELSE c.reltuples::bigint END AS estimated_row_count
                FROM pg_catalog.pg_class AS c
                JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s
                  AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
                  AND c.relkind IN ('r', 'p')
                  AND pg_catalog.has_schema_privilege(n.oid, 'USAGE')
                  AND pg_catalog.has_table_privilege(c.oid, 'SELECT')
                """,
                (schema, table),
            )
        ).fetchone()
        if row is None:
            raise ConnectorError("Table does not exist or SELECT access is not granted")
        return row

    async def _columns(
        self, conn: AsyncConnection[dict[str, Any]], oid: int
    ) -> list[dict[str, Any]]:
        return await (
            await conn.execute(
                """
                SELECT a.attname AS name,
                       pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
                       NOT (a.attnotnull OR t.typnotnull) AS nullable,
                       EXISTS (SELECT 1 FROM pg_catalog.pg_constraint AS k
                               WHERE k.conrelid = a.attrelid AND k.contype = 'p'
                                 AND a.attnum = ANY(k.conkey)) AS primary_key
                FROM pg_catalog.pg_attribute AS a
                JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
                WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped
                ORDER BY a.attnum
                """,
                (oid,),
            )
        ).fetchall()

    async def describe_table(self, schema: str, table: str) -> dict[str, Any]:
        async with self.connect() as conn:
            relation = await self._table(conn, schema, table)
            columns = await self._columns(conn, relation["oid"])
            foreign_keys = await (
                await conn.execute(
                    """
                    SELECT k.conname AS name,
                           array_agg(a.attname ORDER BY key.position) AS columns,
                           n.nspname AS referenced_schema, t.relname AS referenced_table,
                           array_agg(b.attname ORDER BY key.position) AS referenced_columns
                    FROM pg_catalog.pg_constraint AS k
                    JOIN pg_catalog.pg_class AS t ON t.oid = k.confrelid
                    JOIN pg_catalog.pg_namespace AS n ON n.oid = t.relnamespace
                    CROSS JOIN LATERAL unnest(k.conkey, k.confkey) WITH ORDINALITY
                        AS key(source_number, target_number, position)
                    JOIN pg_catalog.pg_attribute AS a
                        ON a.attrelid = k.conrelid AND a.attnum = key.source_number
                    JOIN pg_catalog.pg_attribute AS b
                        ON b.attrelid = k.confrelid AND b.attnum = key.target_number
                    WHERE k.conrelid = %s AND k.contype = 'f'
                    GROUP BY k.oid, k.conname, n.nspname, t.relname
                    ORDER BY k.conname
                    """,
                    (relation["oid"],),
                )
            ).fetchall()
        return {"schema": schema, "table": table, "columns": columns, "foreign_keys": foreign_keys}

    async def sample_rows(self, schema: str, table: str, limit: int = 10) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ConnectorError("limit must be an integer between 1 and 100")
        async with self.connect() as conn:
            await self._table(conn, schema, table)
            rows = await (
                await conn.execute(
                    sql.SQL("SELECT to_jsonb(t.*) AS row FROM {} AS t LIMIT %s").format(
                        sql.Identifier(schema, table)
                    ),
                    (limit,),
                )
            ).fetchall()
        return {"schema": schema, "table": table, "rows": [row["row"] for row in rows]}

    async def read_rows(
        self, schema: str, table: str, offset: int = 0, limit: int = 1000
    ) -> dict[str, Any]:
        if type(offset) is not int or offset < 0:
            raise ConnectorError("offset must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ConnectorError("limit must be an integer between 1 and 5000")
        async with self.connect() as conn:
            relation = await self._table(conn, schema, table)
            columns = await self._columns(conn, relation["oid"])
            primary_key = await (
                await conn.execute(
                    """
                    SELECT a.attname AS name FROM pg_catalog.pg_constraint AS k
                    CROSS JOIN LATERAL unnest(k.conkey) WITH ORDINALITY AS key(attnum, position)
                    JOIN pg_catalog.pg_attribute AS a
                      ON a.attrelid = k.conrelid AND a.attnum = key.attnum
                    WHERE k.conrelid = %s AND k.contype = 'p' ORDER BY key.position
                    """,
                    (relation["oid"],),
                )
            ).fetchall()
            for column in columns:
                validate_identifier(column["name"])
            # to_jsonb is the same conversion as sample_rows and also lets the
            # keyless fallback order types such as json without native ordering.
            ordering = sql.SQL(", ").join(
                sql.Identifier("t", c["name"])
                if primary_key
                else sql.SQL("to_jsonb({})").format(sql.Identifier("t", c["name"]))
                for c in primary_key or columns
            )
            select = sql.SQL("SELECT to_jsonb(t.*) AS row FROM {} AS t{}").format(
                sql.Identifier(schema, table),
                sql.SQL(" ORDER BY {}").format(ordering) if columns else sql.SQL(""),
            )
            # psycopg parses %-placeholders even inside quoted identifiers.
            # Escape those literal signs before adding the bound pagination.
            query = select.as_string(conn).replace("%", "%%") + " LIMIT %s OFFSET %s"
            page = await (await conn.execute(query, (limit + 1, offset))).fetchall()
        return {
            "schema": schema,
            "table": table,
            "columns": [c["name"] for c in columns],
            "rows": [row["row"] for row in page[:limit]],
            "next_offset": offset + limit if len(page) > limit else None,
        }

    async def table_stats(self, schema: str, table: str) -> dict[str, Any]:
        async with self.connect() as conn:
            relation = await self._table(conn, schema, table)
            columns = await self._columns(conn, relation["oid"])
            # One bounded materialized sample shared by every column aggregate.
            # to_jsonb also handles json/array types without a native equality operator.
            aggregates = [
                sql.SQL(
                    "jsonb_build_object('name', %s::text, 'null_fraction', "
                    "count(*) FILTER (WHERE {column} IS NULL)::float8 / nullif(count(*), 0), "
                    "'distinct_count', count(DISTINCT to_jsonb({column})))"
                ).format(column=sql.Identifier(column["name"]))
                for column in columns
            ]
            query = sql.SQL(
                "WITH sample AS MATERIALIZED (SELECT * FROM {table} LIMIT %s) "
                "SELECT count(*) AS sample_row_count, {columns} AS columns FROM sample"
            ).format(
                table=sql.Identifier(schema, table),
                columns=(
                    # PostgreSQL limits function calls to 100 arguments.
                    sql.SQL(" || ").join(
                        sql.SQL("jsonb_build_array({})").format(
                            sql.SQL(", ").join(aggregates[start : start + 50])
                        )
                        for start in range(0, len(aggregates), 50)
                    )
                    if aggregates
                    else sql.SQL("'[]'::jsonb")
                ),
            )
            stats = await (
                await conn.execute(
                    query,
                    (self.settings.postgres_stats_sample_size, *[c["name"] for c in columns]),
                )
            ).fetchone()
            assert stats is not None
            low = [
                c["name"]
                for c in stats["columns"]
                if 0 < (c["distinct_count"] or 0) <= TOP_VALUES_MAX
            ]
            top: dict[str, list[dict[str, Any]]] = {name: [] for name in low}
            if low:
                # Counted by the whole value; only the returned text is shortened.
                counts = sql.SQL(" UNION ALL ").join(
                    sql.SQL(
                        "SELECT %s::text AS name, left(value, %s) AS value, "
                        "length(value) > %s AS truncated, count FROM ("
                        "SELECT {column}::text AS value, count(*) AS count FROM sample "
                        "WHERE {column} IS NOT NULL GROUP BY 1) AS counted"
                    ).format(column=sql.Identifier(name))
                    for name in low
                )
                query = sql.SQL(
                    "WITH sample AS MATERIALIZED (SELECT * FROM {table} LIMIT %s) {counts}"
                ).format(table=sql.Identifier(schema, table), counts=counts)
                params: list[Any] = [self.settings.postgres_stats_sample_size]
                for name in low:
                    params += [name, TOP_VALUE_CHARS, TOP_VALUE_CHARS]
                for row in await (await conn.execute(query, params)).fetchall():
                    value = {"value": row["value"], "count": row["count"]}
                    if row["truncated"]:
                        value["truncated"] = True
                    top[row["name"]].append(value)
            for column in stats["columns"]:
                if column["name"] in top:
                    column["top_values"] = sorted(
                        top[column["name"]], key=lambda v: (-v["count"], v["value"])
                    )
        return {
            "schema": schema,
            "table": table,
            "estimated_row_count": relation["estimated_row_count"],
            "sample_limit": self.settings.postgres_stats_sample_size,
            **stats,
        }
