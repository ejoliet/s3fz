from __future__ import annotations

import multiprocessing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import T0, batch
from s3fz.errors import IndexLocked
from s3fz.index.store import Store, ancestors


def keys_in(store: Store, prefix: str = "") -> list[str]:
    rows = store.conn.execute(
        "SELECT key FROM objects WHERE key >= ? AND key < ? || chr(255) ORDER BY key",
        [prefix, prefix],
    ).fetchall()
    return [r[0] for r in rows]


def test_ancestors() -> None:
    assert ancestors("a/b/c/") == ["a/b/", "a/", ""]
    assert ancestors("a/") == [""]
    assert ancestors("") == []


def test_tree_replace_drops_deleted_objects(store: Store) -> None:
    keys = [f"p/{i:04d}.fits" for i in range(1000)]
    assert store.replace_prefix("p/", [batch(keys)], kind="tree") == 1000
    assert store.count("p/") == 1000
    again = [k for k in keys if k not in {"p/0001.fits", "p/0500.fits", "p/0999.fits"}]
    assert store.replace_prefix("p/", [batch(again)], kind="tree") == 997
    assert keys_in(store, "p/") == again
    assert store.prefix_row("p/")[0] == "warm"
    assert store.prefix_row("p/")[2] == 997


def test_level_replace_keeps_children(store: Store) -> None:
    store.replace_prefix("a/b/", [batch(["a/b/x.txt", "a/b/y.txt"])], kind="tree")
    store.replace_prefix(
        "a/", [batch(["a/1.txt", "a/", "a/2.txt"])], kind="level", child_prefixes=["a/b/", "a/c/"]
    )
    assert keys_in(store) == ["a/1.txt", "a/2.txt", "a/b/x.txt", "a/b/y.txt"]
    store.replace_prefix("a/", [batch(["a/2.txt"])], kind="level", child_prefixes=["a/b/"])
    assert keys_in(store) == ["a/2.txt", "a/b/x.txt", "a/b/y.txt"]
    assert store.prefix_row("a/b/")[0] == "warm"
    assert store.prefix_row("a/c/")[0] == "cold"


def test_replace_is_one_transaction(store: Store) -> None:
    store.replace_prefix("p/", [batch(["p/a", "p/b"])], kind="tree")

    def bad():
        yield batch(["p/c"])
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        store.replace_prefix("p/", bad(), kind="tree")
    assert keys_in(store, "p/") == ["p/a", "p/b"]


@pytest.mark.parametrize(
    ("key", "ext", "ext2"),
    [
        ("d/x.fits", "fits", None),
        ("d/x.FITS.gz", "gz", "fits.gz"),
        ("d/README", None, None),
        ("d/.hidden", None, None),
        ("d/a.b.c", "c", "b.c"),
        ("dir.v2/file", None, None),
        ("r0011010001001_f146_cal.asdf", "asdf", None),
    ],
)
def test_derived_columns(store: Store, key: str, ext: str | None, ext2: str | None) -> None:
    store.replace_prefix("", [batch([key])], kind="tree")
    row = store.conn.execute("SELECT dir, name, ext, ext2, rkey FROM objects").fetchone()
    d, _, n = key.rpartition("/")
    assert row == (d + "/" if d else "", n, ext, ext2, key[::-1])


def test_rollup_propagates_to_root(store: Store) -> None:
    store.replace_prefix("", [batch(["top.txt"])], kind="level", child_prefixes=["a/"])
    store.replace_prefix("a/", [batch(["a/mid.txt"])], kind="level", child_prefixes=["a/b/"])
    newest = datetime(2026, 12, 25, tzinfo=UTC)
    store.replace_prefix("a/b/", [batch(["a/b/deep.txt"], start=newest)], kind="tree")
    for p in ("a/b/", "a/", ""):
        assert store.prefix_row(p)[3] == newest, p
    # a tree listing that lands an older file does not move ancestors backwards
    store.replace_prefix("a/c/", [batch(["a/c/old.txt"], start=T0)], kind="tree")
    assert store.prefix_row("")[3] == newest


def test_tree_marks_intermediate_dirs(store: Store) -> None:
    store.replace_prefix("r/", [batch(["r/l2/v1/f.fits", "r/l2/v2/g.fits"])], kind="tree")
    rows = dict(
        store.conn.execute("SELECT prefix, object_count FROM prefixes ORDER BY prefix").fetchall()
    )
    assert rows == {"r/": 2, "r/l2/": 0, "r/l2/v1/": 1, "r/l2/v2/": 1}
    assert all(store.prefix_row(p)[0] == "warm" for p in rows)
    # object_count for the listed prefix is the whole listing; dirs below count direct objects
    assert (
        store.conn.execute("SELECT max_mtime FROM prefixes WHERE prefix='r/l2/'").fetchone()[0]
        == store.conn.execute("SELECT max(last_modified) FROM objects").fetchone()[0]
    )


def test_obstore_batch_schema_matches_insert(tmp_path: Path) -> None:
    """A real obstore batch (local store) must have the column names INSERT_OBJECTS_SQL expects."""
    import obstore
    from obstore.store import LocalStore

    (tmp_path / "k").mkdir()
    (tmp_path / "k" / "one.fits").write_bytes(b"x")
    stream = obstore.list(LocalStore(tmp_path), "k", return_arrow=True)
    batches = list(stream)
    import pyarrow as pa

    names = set(pa.record_batch(batches[0]).schema.names)
    assert {"path", "size", "last_modified", "e_tag"} <= names
    store = Store.in_memory("b")
    assert store.replace_prefix("k/", batches, kind="tree") == 1


def _open_and_report(cache: str, q: multiprocessing.Queue) -> None:  # type: ignore[type-arg]
    try:
        Store.open("b", cache_dir=Path(cache)).close()
        q.put("opened")
    except IndexLocked as e:
        q.put(("locked", e.holder_pid, e.exit_code))


def test_second_writer_is_locked(tmp_path: Path) -> None:
    import os

    with Store.open("b", cache_dir=tmp_path):
        q: multiprocessing.Queue = multiprocessing.Queue()  # type: ignore[type-arg]
        ctx = multiprocessing.get_context("spawn")
        p = ctx.Process(target=_open_and_report, args=(str(tmp_path), q))
        p.start()
        result = q.get(timeout=60)
        p.join()
    assert result == ("locked", os.getpid(), 3)
    # released after close
    Store.open("b", cache_dir=tmp_path).close()


def test_file_store_persists(tmp_path: Path) -> None:
    with Store.open("b", cache_dir=tmp_path) as s:
        s.replace_prefix("", [batch(["a.txt"])], kind="tree")
    with Store.open("b", cache_dir=tmp_path, read_only=True) as s:
        assert s.count() == 1
        assert s.conn.execute("SELECT version FROM schema_version").fetchone() == (1,)
