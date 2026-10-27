"""Settings from environment variables.

AIDEV-NOTE: this table is the source of truth for docs/configuration.md (Invariant 11).
Each field's description is the user-facing text.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="S3FZ_", extra="ignore")

    cache_dir: Path = Field(default=Path("~/.cache/s3fz"), description="Index files location")
    endpoint_url: str | None = Field(default=None, description="MinIO / non-AWS S3 endpoint")
    ttl: str = Field(default="1h", description="Re-list a warm prefix after this age when viewed")
    parallelism: int = Field(
        default=16, description="Concurrent ListObjectsV2 calls in the crawler"
    )
    max_results: int = Field(default=500, description="Rows pushed to fzf per deep-search query")
    crawl_depth: int | None = Field(
        default=None, description="Background crawl depth below the viewed prefix"
    )
    inventory_autodetect: bool = Field(
        default=True, description="Call GetBucketInventoryConfiguration on open"
    )
    fzf_bin: str = Field(default="fzf", description="Override fzf binary")
    log_level: str = Field(default="WARNING", description="Structured JSON logs to stderr")

    def resolved_cache_dir(self) -> Path:
        return self.cache_dir.expanduser()
