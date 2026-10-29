"""Manifest-only discovery with bounded reads through a locked, read-only DuckDB."""

import json
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import duckdb
from pydantic import BaseModel, Field, ValidationError

from aep_files.config import Settings

TOP_VALUES_MAX = 20
TOP_VALUE_CHARS = 80


class ConnectorError(ValueError):
    """A safe error for MCP callers; raw driver errors may contain source data."""


class Column(BaseModel):
    name: str = Field(min_length=1)
    data_type: str
    nullable: bool = True
    primary_key: bool = False


class Table(BaseModel):
    table: str = Field(min_length=1)
    file: str
    columns: list[Column] = Field(min_length=1)
    row_count: int = Field(ge=0)


class Manifest(BaseModel):
    dataset_id: str
    schema_name: str = Field(alias="schema")
    tables: list[Table] = Field(min_length=1)


def validate_dataset_id(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ConnectorError("dataset_id must be a canonical UUID") from None
    return value


def validate_identifier(value: str) -> str:
    if not value or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ConnectorError(
            "File identifiers must be nonempty and contain no controls or surrogates"
        )
    return value


def quote(value: str) -> str:
    validate_identifier(value)
    return '"' + value.replace('"', '""') + '"'


class Database:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def manifest(self, dataset_id: str) -> tuple[Path, Manifest]:
        validate_dataset_id(dataset_id)
        root = self.settings.uploads_dir.resolve()
        directory = root / dataset_id
        manifest_path = directory / "manifest.json"
        try:
            if directory.is_symlink() or directory.resolve() != directory:
                raise ValueError
            if manifest_path.is_symlink():
                raise ValueError
            manifest = Manifest.model_validate_json(manifest_path.read_bytes())
            if (
                manifest.dataset_id != dataset_id
                or manifest.schema_name != "upload_" + dataset_id[:8]
            ):
                raise ValueError
            if len({t.table.casefold() for t in manifest.tables}) != len(manifest.tables):
                raise ValueError
            for table in manifest.tables:
                path = directory / table.file
                if (
                    not re.fullmatch(r"[^/\\]+\.parquet", table.file)
                    or path.is_symlink()
                    or path.resolve().parent != directory
                    or not path.is_file()
                    or "\x00" in table.table
                    or any("\x00" in c.name for c in table.columns)
                ):
                    raise ValueError
            return directory, manifest
        except (OSError, ValueError, ValidationError):
            raise ConnectorError("Dataset not found or manifest is invalid") from None

    @contextmanager
    def connect(self, dataset_id: str) -> Iterator[duckdb.DuckDBPyConnection]:
        directory, manifest = self.manifest(dataset_id)
        # DuckDB cannot open :memory: read-only. Bootstrap an EMPTY temporary catalog;
        # no uploaded data is copied into it. Every discovery session is read_only=True.
        with tempfile.TemporaryDirectory(prefix="aep-files-") as temp:
            catalog = str(Path(temp) / "empty.duckdb")
            duckdb.connect(catalog).close()
            try:
                with duckdb.connect(
                    catalog,
                    read_only=True,
                    config={
                        "autoinstall_known_extensions": "false",
                        "autoload_known_extensions": "false",
                        "threads": "1",
                        "memory_limit": "256MB",
                    },
                ) as conn:
                    # Exact allowlist, rather than trusting a caller-provided glob/path.
                    conn.execute("SET temp_directory = ''")
                    conn.execute(
                        "SET allowed_paths = ?",
                        [[str(directory / t.file) for t in manifest.tables]],
                    )
                    conn.execute("SET enable_external_access = false")
                    conn.execute("SET lock_configuration = true")
                    yield conn
            except duckdb.Error:
                raise ConnectorError(
                    "File query failed; check dataset files and manifest"
                ) from None

    def _table(self, dataset_id: str, schema: str, table: str) -> tuple[Path, Table]:
        validate_identifier(schema)
        validate_identifier(table)
        directory, manifest = self.manifest(dataset_id)
        if schema == manifest.schema_name:
            for entry in manifest.tables:
                if table == entry.table:
                    return directory / entry.file, entry
        raise ConnectorError("Table does not exist or SELECT access is not granted")

    def list_schemas(self, dataset_id: str) -> dict[str, Any]:
        _, manifest = self.manifest(dataset_id)
        return {"schemas": [manifest.schema_name]}

    def list_tables(self, dataset_id: str, schema: str) -> dict[str, Any]:
        _, manifest = self.manifest(dataset_id)
        if schema != manifest.schema_name:
            raise ConnectorError("Schema does not exist or SELECT access is not granted")
        return {"schema": schema, "tables": sorted(t.table for t in manifest.tables)}

    def describe_table(self, dataset_id: str, schema: str, table: str) -> dict[str, Any]:
        _, entry = self._table(dataset_id, schema, table)
        return {
            "schema": schema,
            "table": table,
            "columns": [c.model_dump() for c in entry.columns],
            "foreign_keys": [],
        }

    def _sample(self, dataset_id: str, schema: str, table: str, limit: int) -> list[dict[str, Any]]:
        path, entry = self._table(dataset_id, schema, table)
        # All identifiers come from the manifest and are still quoted. The file path
        # and row limit are bound parameters, never SQL fragments.
        columns = ", ".join(quote(c.name) for c in entry.columns)
        with self.connect(dataset_id) as conn:
            values = conn.execute(
                f"SELECT {columns} FROM read_parquet(?) AS {quote(table)} LIMIT ?",
                [str(path), limit],
            ).fetchall()
        return [dict(zip([c.name for c in entry.columns], row, strict=True)) for row in values]

    def sample_rows(
        self, dataset_id: str, schema: str, table: str, limit: int = 10
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ConnectorError("limit must be an integer between 1 and 100")
        # Match PostgreSQL to_jsonb: dates become ISO strings, decimals JSON numbers.
        rows = self._sample(dataset_id, schema, table, limit)
        return {
            "schema": schema,
            "table": table,
            "rows": json.loads(json.dumps(rows, default=_json_value)),
        }

    def read_rows(
        self, dataset_id: str, schema: str, table: str, offset: int = 0, limit: int = 1000
    ) -> dict[str, Any]:
        if type(offset) is not int or offset < 0:
            raise ConnectorError("offset must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= 5000:
            raise ConnectorError("limit must be an integer between 1 and 5000")
        path, entry = self._table(dataset_id, schema, table)
        names = [c.name for c in entry.columns]
        primary_key = [c.name for c in entry.columns if c.primary_key]
        projection = ", ".join(quote(name) for name in names)
        ordering = ", ".join(quote(name) for name in primary_key or names)
        with self.connect(dataset_id) as conn:
            values = conn.execute(
                f"SELECT {projection} FROM read_parquet(?) AS {quote(table)} "
                f"ORDER BY {ordering} LIMIT ? OFFSET ?",
                [str(path), limit + 1, offset],
            ).fetchall()
        rows = [dict(zip(names, row, strict=True)) for row in values[:limit]]
        return {
            "schema": schema,
            "table": table,
            "columns": names,
            "rows": json.loads(json.dumps(rows, default=_json_value)),
            "next_offset": offset + limit if len(values) > limit else None,
        }

    def table_stats(self, dataset_id: str, schema: str, table: str) -> dict[str, Any]:
        _, entry = self._table(dataset_id, schema, table)
        limit = self.settings.files_stats_sample_size
        rows = self._sample(dataset_id, schema, table, limit)
        columns: list[dict[str, Any]] = []
        for column in entry.columns:
            values = [row[column.name] for row in rows]
            counts: dict[str, int] = {}
            for value in values:
                if value is not None:
                    # Keep whole values distinct before shortening the returned text.
                    text = _pg_text(value)
                    counts[text] = counts.get(text, 0) + 1
            stats: dict[str, Any] = {
                "name": column.name,
                "null_fraction": values.count(None) / len(values) if values else None,
                "distinct_count": len(counts),
            }
            if 0 < len(counts) <= TOP_VALUES_MAX:
                top = [
                    {
                        "value": value[:TOP_VALUE_CHARS],
                        "count": count,
                        **({"truncated": True} if len(value) > TOP_VALUE_CHARS else {}),
                    }
                    for value, count in counts.items()
                ]
                stats["top_values"] = sorted(top, key=lambda v: (-v["count"], v["value"]))
            columns.append(stats)
        return {
            "schema": schema,
            "table": table,
            "estimated_row_count": entry.row_count,
            "sample_limit": limit,
            "sample_row_count": len(rows),
            "columns": columns,
        }


def _json_value(value: Any) -> Any:
    from datetime import date, datetime, time
    from decimal import Decimal

    if isinstance(value, date | datetime | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def _pg_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
