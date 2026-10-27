from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import quote

import duckdb
import pytest

from s3fz.index.inventory import InventoryError, load_inventory, manifest_time, parse_manifest
from s3fz.index.store import Store

SCHEMA = "Bucket, Key, Size, LastModifiedDate, ETag, StorageClass"
ROWS = [
    ("b", "roman/l2/r001_f146_cal.asdf", 10, "2026-10-01T00:00:00.000Z", "e1", "STANDARD"),
    ("b", "roman/l2/r002 x_f158_cal.asdf", 20, "2026-10-02T00:00:00.000Z", "e2", "GLACIER"),
    ("b", "spherex/q.fits.gz", 30, "2026-10-03T00:00:00.000Z", "e3", "STANDARD"),
]


def manifest(fmt: str, files: list[str]) -> dict:
    return {
        "sourceBucket": "b",
        "destinationBucket": "arn:aws:s3:::inv",
        "version": "2016-11-30",
        "creationTimestamp": "1761350400000",
        "fileFormat": fmt,
        "fileSchema": SCHEMA,
        "files": [{"key": f, "size": 1, "MD5checksum": "x"} for f in files],
    }


@pytest.fixture
def parquet_files(tmp_path: Path) -> list[str]:
    con = duckdb.connect()
    out = []
    for i, rows in enumerate((ROWS[:2], ROWS[2:])):
        con.execute(
            "CREATE OR REPLACE TABLE t (bucket VARCHAR, key VARCHAR, size BIGINT, "
            "last_modified_date TIMESTAMP, e_tag VARCHAR, storage_class VARCHAR)"
        )
        con.executemany("INSERT INTO t VALUES (?, ?, ?, ?::TIMESTAMP, ?, ?)", rows)
        f = tmp_path / f"part{i}.parquet"
        con.execute(f"COPY t TO '{f}' (FORMAT parquet)")
        out.append(str(f))
    return out


@pytest.fixture
def csv_file(tmp_path: Path) -> str:
    f = tmp_path / "part.csv.gz"
    lines = [
        ",".join(f'"{quote(str(v))}"' if j == 1 else f'"{v}"' for j, v in enumerate(r))
        for r in ROWS
    ]
    f.write_bytes(gzip.compress(("\n".join(lines) + "\n").encode()))
    return str(f)


def test_parquet_load(parquet_files: list[str]) -> None:
    store = Store.in_memory("b")
    n = load_inventory(store, manifest("Parquet", parquet_files), lambda k: k)
    assert n == 3
    rows = store.conn.execute(
        "SELECT key, size, ext, ext2, storage_class, etag FROM objects ORDER BY key"
    ).fetchall()
    assert rows[0] == ("roman/l2/r001_f146_cal.asdf", 10, "asdf", None, "STANDARD", "e1")
    assert rows[2] == ("spherex/q.fits.gz", 30, "gz", "fits.gz", "STANDARD", "e3")
    status, listed_at, count, _, source = store.prefix_row("")
    assert (status, count, source) == ("warm", 3, "inventory")
    assert listed_at == manifest_time(manifest("Parquet", []))
    assert store.prefix_row("roman/l2/")[0] == "warm"
    assert store.prefix_row("roman/l2/")[4] == "inventory"
    assert (
        store.prefix_row("roman/")[3]
        == store.conn.execute(
            "SELECT max(last_modified) FROM objects WHERE key LIKE 'roman/%'"
        ).fetchone()[0]
    )


def test_csv_load_url_decodes_keys(csv_file: str) -> None:
    store = Store.in_memory("b")
    assert load_inventory(store, manifest("CSV", [csv_file]), lambda k: k) == 3
    keys = [r[0] for r in store.conn.execute("SELECT key FROM objects ORDER BY key").fetchall()]
    assert "roman/l2/r002 x_f158_cal.asdf" in keys


def test_scoped_load_replaces_only_prefix(parquet_files: list[str]) -> None:
    store = Store.in_memory("b")
    from conftest import batch

    store.replace_prefix("spherex/", [batch(["spherex/stale.fits"])], kind="tree")
    store.replace_prefix("other/", [batch(["other/keep.fits"])], kind="tree")
    n = load_inventory(store, manifest("Parquet", parquet_files), lambda k: k, prefix="spherex/")
    assert n == 1
    keys = [r[0] for r in store.conn.execute("SELECT key FROM objects ORDER BY key").fetchall()]
    assert keys == ["other/keep.fits", "spherex/q.fits.gz"]


def test_manifest_validation() -> None:
    with pytest.raises(InventoryError, match="ORC"):
        parse_manifest(json.dumps(manifest("ORC", ["x"])))
    with pytest.raises(InventoryError, match="no files"):
        parse_manifest(json.dumps(manifest("Parquet", [])))
    m = manifest("Parquet", ["x"])
    m["fileSchema"] = "Bucket, Key"
    with pytest.raises(InventoryError, match="required"):
        load_inventory(Store.in_memory("b"), m, lambda k: k)


def test_cli_inventory_from_s3(
    aws_env: str, parquet_files: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import boto3

    from s3fz.cli import main

    s3 = boto3.client("s3", endpoint_url=aws_env)
    s3.create_bucket(Bucket="inv")
    keys = []
    for i, f in enumerate(parquet_files):
        keys.append(f"b/daily/data/part{i}.parquet")
        s3.upload_file(f, "inv", keys[-1])
    s3.put_object(
        Bucket="inv", Key="b/daily/manifest.json", Body=json.dumps(manifest("Parquet", keys))
    )
    assert main(["inventory", "s3://b/", "--from", "s3://inv/b/daily/manifest.json"]) == 0
    assert capsys.readouterr().out.strip() == "3"
    with Store.open("b", cache_dir=tmp_path, read_only=True) as store:
        assert store.count() == 3
        assert store.prefix_row("roman/l2/")[4] == "inventory"
