from __future__ import annotations

from pathlib import Path

import pytest

from s3fz.cli import main


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    assert "s3fz" in capsys.readouterr().out


def test_cache_path_and_rm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("S3FZ_CACHE_DIR", str(tmp_path))
    assert main(["cache", "path", "s3://bkt/x/"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "bkt.duckdb")
    assert main(["cache", "rm", "s3://bkt/"]) == 0
    assert not (tmp_path / "bkt.duckdb").exists()
    assert main(["cache", "ls"]) == 0
    assert main(["cache", "path", "nope"]) == 1
