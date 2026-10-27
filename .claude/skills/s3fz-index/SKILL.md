---
name: s3fz-index
description: How to build and change the s3fz DuckDB index (src/s3fz/index/) — schema, Arrow batch ingest, prefix-replace refresh, max_mtime rollups, single-owner locking, S3 Inventory loading, migrations. Use whenever touching index/, the DuckDB file, schema columns, ingest speed, stale or missing objects, "sort folders by modified", or Inventory parsing.
---

# s3fz index

One DuckDB file per bucket at `$S3FZ_CACHE_DIR/<bucket>.duckdb`, owned by exactly one process.

## Schema (source of truth: `index/schema.py`)

- `objects(bucket, key, dir, name, ext, ext2, rkey, size, last_modified, etag, storage_class, seen_at)` — no PK, no ART indexes.
- `prefixes(bucket, prefix, status, listed_at, object_count, max_mtime, source)` with PK `(bucket, prefix)`. Small table; PK is fine here.

Derived columns are computed in SQL during insert, never in Python:

```sql
INSERT INTO objects
SELECT ?, path,
       regexp_extract(path, '^(.*/)', 1)                                        AS dir,
       regexp_extract(path, '[^/]*$')                                           AS name,
       lower(nullif(regexp_extract(regexp_extract(path, '[^/]*$'), '\.([^.]+)$', 1), ''))        AS ext,
       lower(nullif(regexp_extract(regexp_extract(path, '[^/]*$'), '\.([^.]+\.[^.]+)$', 1), '')) AS ext2,
       reverse(path)                                                            AS rkey,
       size, last_modified, e_tag, NULL, now()
FROM batch ORDER BY path;
```

obstore Arrow column names are expected to be `path`, `size`, `last_modified`, `e_tag`. Assert them in a test against a real batch from moto, so a library change fails loudly.

## Ingest

- Wrap obstore `arro3` batches with `pyarrow.record_batch(b)` (zero-copy via the Arrow C interface) and `conn.register("batch", pa_batch)`. Unregister after insert.
- One writer thread, one connection. Commit per completed prefix so a crash loses at most one prefix.
- `AIDEV-PERF:` no PK and no ART indexes on `objects`. Key-sorted inserts give zone maps; write prefix scope as a range, `key >= ? AND key < ? || chr(255)`, which zone maps prune. Do not rely on `starts_with` being pushed down; check with `EXPLAIN ANALYZE` in the perf test. Add an index only with a failing perf test that proves it helps.
- Bulk-load settings: `SET preserve_insertion_order = false`; leave `threads` at default (all cores).

## Refresh = prefix replace (Invariant 4)

```sql
BEGIN;
-- delimiter (one-level) refresh:
DELETE FROM objects WHERE bucket = ? AND dir = ?;
-- recursive refresh instead:
-- DELETE FROM objects WHERE bucket = ? AND starts_with(key, ?);
INSERT ...;
UPDATE prefixes SET status = 'warm', listed_at = now(), object_count = ? WHERE bucket = ? AND prefix = ?;
COMMIT;
```

The two DELETE forms are not interchangeable. A recursive delete after a one-level listing wipes children that were not re-listed.

## max_mtime rollup

After each prefix completes, update its ancestor chain bottom-up in one SQL statement:
`max_mtime(p) = greatest(max(last_modified) of objects with dir = p, max(max_mtime) of direct child prefixes)`.
Compute the ancestor list in Python (string splitting, a handful of items), run the update in SQL. This powers "sort folders by modified".

## Single owner (Invariant 2)

- Lock file `<bucket>.lock` with PID; `fcntl.flock(LOCK_EX | LOCK_NB)`. On failure: exit 3, print holder PID.
- `s3fz query --read-only` opens `read_only=True` only when no lock holder exists.

## Inventory loader

Manifest `fileFormat` is `CSV`, `ORC`, or `Parquet`. Support Parquet and gzipped CSV (no header, columns from `fileSchema`). ORC → clear error recommending Parquet. Read from S3 with DuckDB `httpfs` and `CREATE SECRET (TYPE s3, PROVIDER credential_chain)`. CSV Inventory keys are URL-encoded: `url_decode()` them. Insert through the same derived-column SQL as live batches.

## Migrations

`schema_version` table. On mismatch run forward migrations; if impossible, rename the file to `.bak`, rebuild, and say so on stderr.

## Tests

- Insert 1,000 keys, re-list with 3 deleted → exactly 997 remain.
- Delimiter refresh of `a/` leaves `a/b/` rows intact.
- `ext`/`ext2` for `x.fits`, `x.fits.gz`, `README`, `.hidden`, `a.b.c`, `dir.v2/file`.
- Rollup: deepest file mtime propagates to root.
- Perf (`-m perf`): 1 M synthetic rows; `name ILIKE '%f146%' ORDER BY last_modified DESC LIMIT 500` < 200 ms.
- Second writer process → exit 3.
