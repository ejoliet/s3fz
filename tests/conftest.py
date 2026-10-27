from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from s3fz.index.store import Store

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def batch(keys: list[str], *, start: datetime = T0, step_minutes: int = 1) -> pa.RecordBatch:
    """Arrow batch shaped like obstore's list output. mtime increases with position."""
    n = len(keys)
    return pa.record_batch(
        {
            "path": pa.array(keys, pa.string()),
            "last_modified": pa.array(
                [start + timedelta(minutes=i * step_minutes) for i in range(n)],
                pa.timestamp("us", "UTC"),
            ),
            "size": pa.array([len(k) for k in keys], pa.int64()),
            "e_tag": pa.array([f'"{i:x}"' for i in range(n)], pa.string()),
            "version": pa.array([None] * n, pa.string()),
        }
    )


@pytest.fixture
def store() -> Store:
    return Store.in_memory("b")


@pytest.fixture(scope="session")
def moto_endpoint() -> Iterator[str]:
    """moto in server mode: obstore and DuckDB httpfs speak HTTP, in-process mocks don't apply."""
    import socket
    import subprocess
    import sys
    import time

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        [sys.executable, "-m", "moto.server", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("moto_server did not start")
    yield url
    proc.kill()
    proc.wait()


@pytest.fixture
def aws_env(moto_endpoint: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    for k, v in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "S3FZ_ENDPOINT_URL": moto_endpoint,
        "S3FZ_CACHE_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(k, v)
    for k in ("AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    return moto_endpoint
