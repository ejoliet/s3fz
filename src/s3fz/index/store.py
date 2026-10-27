"""Single-owner DuckDB index store: prefix replace, prefix status, max_mtime rollup."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import duckdb

from s3fz.config import Settings
from s3fz.errors import IndexLocked
from s3fz.index.schema import INSERT_OBJECTS_SQL, ensure_schema

Kind = Literal["level", "tree"]
Source = Literal["live", "inventory", "shared"]

# AIDEV-PERF: range form `key >= p AND key < p || chr(255)` is pruned by zone maps on
# key-sorted data; starts_with() may not be pushed down (README index schema notes).
SCOPE_SQL = "key >= ? AND key < ? || chr(255)"


def ancestors(prefix: str) -> list[str]:
    """'a/b/c/' -> ['a/b/', 'a/', '']. Root '' has no ancestors."""
    out: list[str] = []
    p = prefix.rstrip("/")
    while p:
        p = p.rpartition("/")[0]
        out.append(p + "/" if p else "")
    return out


class Store:
    """One process owns the DuckDB file (Invariant 2). Use as a context manager."""

    def __init__(
        self, bucket: str, conn: duckdb.DuckDBPyConnection, lock_fd: int | None, path: Path
    ) -> None:
        self.bucket = bucket
        self.conn = conn
        self.path = path
        self._lock_fd = lock_fd

    @classmethod
    def open(
        cls,
        bucket: str,
        *,
        cache_dir: Path | None = None,
        read_only: bool = False,
    ) -> Store:
        cache_dir = cache_dir or Settings().resolved_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{bucket}.duckdb"
        lock_fd = None if read_only else _acquire_lock(path.with_suffix(".lock"))
        try:
            conn = duckdb.connect(str(path), read_only=read_only)
        except duckdb.IOException as e:  # another process already holds the DuckDB file
            if lock_fd is not None:
                os.close(lock_fd)
            raise IndexLocked(str(path), None) from e
        conn.execute("SET TimeZone = 'UTC'")
        if not read_only:
            conn.execute("SET preserve_insertion_order = false")
            ensure_schema(conn)
        return cls(bucket, conn, lock_fd, path)

    @classmethod
    def in_memory(cls, bucket: str) -> Store:
        conn = duckdb.connect(":memory:")
        conn.execute("SET TimeZone = 'UTC'")
        ensure_schema(conn)
        return cls(bucket, conn, None, Path(":memory:"))

    def close(self) -> None:
        self.conn.close()
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- writes -------------------------------------------------------------------------

    def replace_prefix(
        self,
        prefix: str,
        batches: Iterable[Any],
        *,
        kind: Kind,
        source: Source = "live",
        child_prefixes: Iterable[str] = (),
        listed_at: datetime | None = None,
    ) -> int:
        """Replace the rows of one listing in a single transaction (Invariant 4).

        kind='level': rows whose dir == prefix (delimiter listing); child_prefixes become
        cold `prefixes` rows. kind='tree': every row under prefix (recursive listing); every
        dir below prefix becomes a warm `prefixes` row.
        Each batch is any Arrow-compatible object with columns path, size, last_modified,
        e_tag (obstore's list schema) and optionally storage_class.
        Returns the number of rows inserted.
        """
        c = self.conn
        c.execute("BEGIN")
        try:
            if kind == "level":
                c.execute("DELETE FROM objects WHERE bucket = ? AND dir = ?", [self.bucket, prefix])
            else:
                c.execute(
                    f"DELETE FROM objects WHERE bucket = ? AND {SCOPE_SQL}",
                    [self.bucket, prefix, prefix],
                )
            inserted = 0
            for batch in batches:
                c.register("batch", _with_storage_class(batch))
                row = c.execute(INSERT_OBJECTS_SQL.format(src="batch"), [self.bucket]).fetchone()
                inserted += int(row[0]) if row else 0
                c.unregister("batch")
            c.execute(
                """
                INSERT INTO prefixes (bucket, prefix, status, listed_at, object_count, source)
                VALUES (?, ?, 'warm', coalesce(?, now()), ?, ?)
                ON CONFLICT (bucket, prefix) DO UPDATE SET
                  status = 'warm', listed_at = excluded.listed_at,
                  object_count = excluded.object_count, source = excluded.source
                """,
                [self.bucket, prefix, listed_at, inserted, source],
            )
            if kind == "level":
                for child in child_prefixes:
                    c.execute(
                        "INSERT OR IGNORE INTO prefixes (bucket, prefix, status) "
                        "VALUES (?, ?, 'cold')",
                        [self.bucket, child],
                    )
            else:
                self.mark_tree_dirs(prefix, source, listed_at=listed_at)
            self.rollup(prefix)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return inserted

    def mark_prefix(self, prefix: str, status: str) -> None:
        self.conn.execute(
            """
            INSERT INTO prefixes (bucket, prefix, status) VALUES (?, ?, ?)
            ON CONFLICT (bucket, prefix) DO UPDATE SET status = excluded.status
            """,
            [self.bucket, prefix, status],
        )

    def mark_tree_dirs(
        self, prefix: str, source: str, *, listed_at: datetime | None = None
    ) -> None:
        # Every dir strictly below prefix (and intermediate dirs with no direct objects) is
        # fully known after a recursive listing, so it gets a warm row with its own max_mtime.
        self.conn.execute(
            f"""
            INSERT INTO prefixes
              (bucket, prefix, status, listed_at, object_count, max_mtime, source)
            WITH RECURSIVE d(p) AS (
              SELECT DISTINCT dir FROM objects WHERE bucket = ? AND {SCOPE_SQL} AND dir <> ?
              UNION
              SELECT regexp_extract(rtrim(p, '/'), '^(.*/)', 1) FROM d
              WHERE length(p) > length(?) AND regexp_extract(rtrim(p, '/'), '^(.*/)', 1) <> ?
            )
            SELECT ?, p, 'warm', coalesce(?, now()),
                   (SELECT count(*) FROM objects o WHERE o.bucket = ? AND o.dir = d.p),
                   (SELECT max(last_modified) FROM objects o
                     WHERE o.bucket = ? AND o.key >= d.p AND o.key < d.p || chr(255)),
                   ?
            FROM d
            ON CONFLICT (bucket, prefix) DO UPDATE SET
              status = 'warm', listed_at = excluded.listed_at,
              object_count = excluded.object_count, max_mtime = excluded.max_mtime,
              source = excluded.source
            """,
            [
                self.bucket,
                prefix,
                prefix,
                prefix,
                prefix,
                prefix,
                self.bucket,
                listed_at,
                self.bucket,
                self.bucket,
                source,
            ],
        )

    def rollup(self, prefix: str) -> None:
        # max_mtime(p) = greatest(newest object directly in p, newest max_mtime of child prefixes).
        # Ancestor list is a handful of strings; the work is in SQL.
        for p in [prefix, *ancestors(prefix)]:
            self.conn.execute(
                """
                UPDATE prefixes SET max_mtime = (
                  SELECT greatest(
                    (SELECT max(last_modified) FROM objects WHERE bucket = ? AND dir = ?),
                    (SELECT max(max_mtime) FROM prefixes
                      WHERE bucket = ? AND prefix <> ?
                        AND regexp_extract(rtrim(prefix, '/'), '^(.*/)', 1) = ?))
                ) WHERE bucket = ? AND prefix = ?
                """,
                [self.bucket, p, self.bucket, p, p, self.bucket, p],
            )

    # ---- reads --------------------------------------------------------------------------

    def prefix_row(self, prefix: str) -> tuple[Any, ...] | None:
        return self.conn.execute(
            "SELECT status, listed_at, object_count, max_mtime, source FROM prefixes "
            "WHERE bucket = ? AND prefix = ?",
            [self.bucket, prefix],
        ).fetchone()

    def count(self, prefix: str = "") -> int:
        row = self.conn.execute(
            f"SELECT count(*) FROM objects WHERE bucket = ? AND {SCOPE_SQL}",
            [self.bucket, prefix, prefix],
        ).fetchone()
        return int(row[0]) if row else 0


def _with_storage_class(batch: Any) -> Any:
    """obstore list batches lack storage_class; add a NULL column so one INSERT fits all."""
    import pyarrow as pa

    pb = batch if isinstance(batch, pa.RecordBatch) else pa.record_batch(batch)
    if "storage_class" in pb.schema.names:
        return pb
    return pb.append_column("storage_class", pa.nulls(pb.num_rows, pa.string()))


def _acquire_lock(lock_path: Path) -> int:
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = os.read(fd, 32).decode(errors="replace").strip()
        os.close(fd)
        raise IndexLocked(str(lock_path), int(holder) if holder.isdigit() else None) from None
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd
