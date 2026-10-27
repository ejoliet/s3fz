"""S3 Inventory loader: manifest.json -> read_parquet / read_csv -> objects."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import duckdb

from s3fz.index.schema import INSERT_OBJECTS_SQL
from s3fz.index.store import Store

log = logging.getLogger("s3fz.inventory")

# fileSchema field (lowercased) -> (Parquet column name, column expected by INSERT_OBJECTS_SQL).
# AWS writes Parquet inventories with snake_case columns; CSV has no header.
_COLUMNS = {
    "key": ("key", "path"),
    "size": ("size", "size"),
    "lastmodifieddate": ("last_modified_date", "last_modified"),
    "etag": ("e_tag", "e_tag"),
    "storageclass": ("storage_class", "storage_class"),
}
_REQUIRED = ("path", "size", "last_modified")


class InventoryError(ValueError):
    pass


def parse_manifest(text: str) -> dict[str, Any]:
    m = json.loads(text)
    fmt = str(m.get("fileFormat", "")).lower()
    if fmt == "orc":
        raise InventoryError("ORC inventories are not supported; configure Parquet or CSV")
    if fmt not in ("parquet", "csv"):
        raise InventoryError(f"unknown inventory fileFormat {m.get('fileFormat')!r}")
    if not m.get("files"):
        raise InventoryError("inventory manifest lists no files")
    return dict(m)


def manifest_time(m: dict[str, Any]) -> datetime:
    return datetime.fromtimestamp(int(m["creationTimestamp"]) / 1000, tz=UTC)


def _select_sql(m: dict[str, Any], prefix: str) -> str:
    """SELECT over the inventory files exposing path/size/last_modified/e_tag/storage_class.

    Column names come from the manifest's fileSchema, so Parquet and CSV map the same way.
    Parameters, in order: file URL list, prefix.
    """
    schema = [c.strip() for c in str(m["fileSchema"]).split(",")]
    fmt = str(m["fileFormat"]).lower()
    exprs: list[str] = []
    seen: set[str] = set()
    for i, field in enumerate(schema):
        mapping = _COLUMNS.get(field.lower())
        if not mapping:
            continue
        pq_col, target = mapping
        seen.add(target)
        src = f'"{pq_col}"' if fmt == "parquet" else f"column{i}"
        if target == "path" and fmt == "csv":
            src = f"url_decode({src})"  # CSV inventory keys are URL-encoded
        if target == "last_modified":
            src = f"({src})::TIMESTAMPTZ"
        if target == "size":
            src = f"({src})::BIGINT"
        exprs.append(f"{src} AS {target}")
    missing = [c for c in _REQUIRED if c not in seen]
    if missing:
        raise InventoryError(f"inventory fileSchema lacks required fields for {missing}")
    for col in ("storage_class", "e_tag"):
        if col not in seen:
            exprs.append(f"NULL::VARCHAR AS {col}")
    reader = (
        "read_parquet(?)"
        if fmt == "parquet"
        else "read_csv(?, header = false, compression = 'gzip', all_varchar = true)"
    )
    cols = ", ".join(exprs)
    return f"SELECT * FROM (SELECT {cols} FROM {reader}) WHERE starts_with(path, ?)"


def load_inventory(
    store: Store,
    manifest: dict[str, Any],
    file_url: Callable[[str], str],
    *,
    prefix: str = "",
) -> int:
    """Replace everything under `prefix` with the inventory contents (Invariant 4).

    file_url maps a manifest `files[].key` to a URL DuckDB can read (s3://... or a local path).
    Returns the number of rows inserted. Prefixes get source='inventory', listed_at = manifest
    time.
    """
    urls = [file_url(f["key"]) for f in manifest["files"]]
    select_sql = _select_sql(manifest, prefix)
    when = manifest_time(manifest)
    c = store.conn
    c.execute("BEGIN")
    try:
        c.execute(
            "DELETE FROM objects WHERE bucket = ? AND key >= ? AND key < ? || chr(255)",
            [store.bucket, prefix, prefix],
        )
        row = c.execute(
            INSERT_OBJECTS_SQL.format(src=f"({select_sql}) inv"), [store.bucket, urls, prefix]
        ).fetchone()
        inserted = int(row[0]) if row else 0
        c.execute(
            """
            INSERT INTO prefixes (bucket, prefix, status, listed_at, object_count, source)
            VALUES (?, ?, 'warm', ?, ?, 'inventory')
            ON CONFLICT (bucket, prefix) DO UPDATE SET status = 'warm',
              listed_at = excluded.listed_at, object_count = excluded.object_count,
              source = 'inventory'
            """,
            [store.bucket, prefix, when, inserted],
        )
        store.mark_tree_dirs(prefix, "inventory", listed_at=when)
        store.rollup(prefix)
        c.execute("COMMIT")
    except duckdb.Error:
        c.execute("ROLLBACK")
        raise
    log.info("inventory loaded: %d rows", inserted)
    return inserted
