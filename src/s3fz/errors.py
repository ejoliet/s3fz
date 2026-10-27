"""Error classes mapped to exit codes (README: 0 ok, 1 usage, 2 auth, 3 locked, 4 fzf, 5 pro)."""

from __future__ import annotations


class S3fzError(Exception):
    exit_code = 1


class AwsAccessError(S3fzError):
    exit_code = 2


class IndexLocked(S3fzError):  # noqa: N818 - name is part of the README contract
    exit_code = 3

    def __init__(self, path: str, holder_pid: int | None) -> None:
        who = f"pid {holder_pid}" if holder_pid else "another process"
        super().__init__(f"index {path} is locked by {who}")
        self.holder_pid = holder_pid


class FzfMissing(S3fzError):  # noqa: N818
    exit_code = 4
