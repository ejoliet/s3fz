"""DDL and migrations for the per-bucket index file."""

from __future__ import annotations

import duckdb

SCHEMA_VERSION = 1

# AIDEV-PERF: no PRIMARY KEY and no ART index on objects. Rows are inserted sorted by key,
# so zone maps serve range scans on key; indexes slowed 10 M-row bulk loads (README).
DDL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS objects (
  bucket        VARCHAR NOT NULL,
  key           VARCHAR NOT NULL,
  dir           VARCHAR NOT NULL,
  name          VARCHAR NOT NULL,
  ext           VARCHAR,
  ext2          VARCHAR,
  size          BIGINT  NOT NULL,
  last_modified TIMESTAMPTZ NOT NULL,
  rkey          VARCHAR NOT NULL,
  etag          VARCHAR,
  storage_class VARCHAR,
  seen_at       TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS prefixes (
  bucket       VARCHAR NOT NULL,
  prefix       VARCHAR NOT NULL,
  status       VARCHAR NOT NULL,
  listed_at    TIMESTAMPTZ,
  object_count BIGINT,
  max_mtime    TIMESTAMPTZ,
  source       VARCHAR,
  PRIMARY KEY (bucket, prefix)
);
"""

# Derived columns are computed in SQL, never in Python (Invariant 3).
# Input relation must expose: path, size, last_modified, e_tag, and optionally storage_class.
# ext: text after the last '.' of name, requiring a char before the dot, so '.hidden' and
# 'README' have no ext. ext2: last two extensions ('fits.gz').
INSERT_OBJECTS_SQL = """
INSERT INTO objects
SELECT ?,
       path,
       regexp_extract(path, '^(.*/)', 1)                                            AS dir,
       regexp_extract(path, '[^/]*$')                                               AS name,
       lower(nullif(regexp_extract(regexp_extract(path, '[^/]*$'),
                                   '[^.]\\.([^.]+)$', 1), ''))                      AS ext,
       lower(nullif(regexp_extract(regexp_extract(path, '[^/]*$'),
                                   '[^.]\\.([^.]+\\.[^.]+)$', 1), ''))              AS ext2,
       size,
       last_modified,
       reverse(path)                                                                AS rkey,
       e_tag,
       storage_class,
       now()
FROM {src}
WHERE path NOT LIKE '%/'
ORDER BY path
"""


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> int:
    """Create tables if missing. Returns the version found in the file (0 if new)."""
    conn.execute(DDL)
    row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    found = int(row[0]) if row and row[0] is not None else 0
    if found == 0:
        conn.execute("INSERT INTO schema_version VALUES (?)", [SCHEMA_VERSION])
        return 0
    if found > SCHEMA_VERSION:
        msg = f"index schema {found} is newer than this s3fz ({SCHEMA_VERSION})"
        raise RuntimeError(msg)
    # shortcut: no forward migrations exist yet; add them here when SCHEMA_VERSION bumps.
    return found
