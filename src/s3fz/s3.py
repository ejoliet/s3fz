"""S3 access helpers: URI parsing, obstore store construction, DuckDB httpfs credentials.

Credentials come only from the AWS chain (Invariant 6). Read-only API surface (Invariant 1).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import boto3
import duckdb
from obstore.auth.boto3 import Boto3CredentialProvider
from obstore.store import S3Store

from s3fz.config import Settings


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """'s3://b/p/x' -> ('b', 'p/x'). Prefix keeps no leading '/'."""
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3:// URI: {uri}")
    bucket, _, key = uri[5:].partition("/")
    if not bucket:
        raise ValueError(f"missing bucket in {uri}")
    return bucket, key


def make_store(bucket: str, settings: Settings | None = None) -> S3Store:
    settings = settings or Settings()
    session = boto3.Session()
    endpoint = settings.endpoint_url
    kwargs: dict[str, Any] = {
        "credential_provider": Boto3CredentialProvider(session),
        "client_options": {
            "timeout": "30s",
            "allow_http": bool(endpoint and endpoint.startswith("http://")),
        },
        "retry_config": {
            "max_retries": 8,
            "retry_timeout": timedelta(seconds=120),
            "backoff": {
                "base": 2.0,
                "init_backoff": timedelta(milliseconds=500),
                "max_backoff": timedelta(seconds=30),
            },
        },
    }
    if session.region_name:
        kwargs["region"] = session.region_name
    if endpoint:
        kwargs["endpoint"] = endpoint
    return S3Store(bucket, **kwargs)


def configure_duckdb_s3(conn: duckdb.DuckDBPyConnection, settings: Settings | None = None) -> None:
    """Let read_parquet/read_csv read s3:// URLs with the same credential chain."""
    settings = settings or Settings()
    conn.execute("INSTALL httpfs; LOAD httpfs;")
    opts = ["TYPE s3", "PROVIDER credential_chain"]
    session = boto3.Session()
    if session.region_name:
        opts.append(f"REGION '{session.region_name}'")
    if settings.endpoint_url:
        host = settings.endpoint_url.split("://", 1)[1]
        opts += [
            f"ENDPOINT '{host}'",
            "URL_STYLE 'path'",
            f"USE_SSL {str(settings.endpoint_url.startswith('https')).lower()}",
        ]
    conn.execute(f"CREATE OR REPLACE SECRET s3fz_s3 ({', '.join(opts)})")
