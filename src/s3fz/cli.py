"""Command-line entry point. stdout = results only; logs are JSON on stderr."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

import obstore

from s3fz import __version__, log
from s3fz.config import Settings
from s3fz.errors import S3fzError

logger = logging.getLogger("s3fz.cli")

EXIT_CODES = {
    0: "ok",
    1: "usage",
    2: "AWS auth/permission",
    3: "index locked",
    4: "fzf missing",
    5: "Pro licence",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="s3fz",
        description="fzf-driven S3 explorer backed by a lazily built DuckDB index.",
        epilog=(
            "examples:\n"
            "  s3fz inventory s3://my-bucket/ --from s3://inv-bucket/my-bucket/daily/2026-10-25T01-00Z/manifest.json\n"
            "  s3fz cache ls\n"
            "  s3fz cache path s3://my-bucket/\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"s3fz {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("inventory", help="bulk-load an S3 Inventory into the bucket index")
    inv.add_argument("uri", metavar="S3URI", help="s3://bucket/[prefix/] to load")
    inv.add_argument(
        "--from",
        dest="manifest",
        required=True,
        metavar="MANIFEST_S3URI",
        help="s3:// URI of the inventory manifest.json",
    )
    inv.set_defaults(fn=cmd_inventory)

    cache = sub.add_parser("cache", help="manage index files")
    csub = cache.add_subparsers(dest="cache_cmd", required=True)
    csub.add_parser("ls", help="list index files").set_defaults(fn=cmd_cache_ls)
    rm = csub.add_parser("rm", help="delete the index for a bucket")
    rm.add_argument("uri", metavar="S3URI")
    rm.set_defaults(fn=cmd_cache_rm)
    path = csub.add_parser("path", help="print the index file path for a bucket")
    path.add_argument("uri", metavar="S3URI")
    path.set_defaults(fn=cmd_cache_path)
    return p


def cmd_cache_ls(args: argparse.Namespace, settings: Settings) -> int:
    d = settings.resolved_cache_dir()
    for f in sorted(d.glob("*.duckdb")) if d.is_dir() else []:
        sys.stdout.write(f"{f.stem}\t{f.stat().st_size}\t{f}\n")
    return 0


def cmd_cache_path(args: argparse.Namespace, settings: Settings) -> int:
    from s3fz.s3 import parse_s3_uri

    bucket, _ = parse_s3_uri(args.uri)
    sys.stdout.write(f"{settings.resolved_cache_dir() / (bucket + '.duckdb')}\n")
    return 0


def cmd_cache_rm(args: argparse.Namespace, settings: Settings) -> int:
    from s3fz.index.store import Store
    from s3fz.s3 import parse_s3_uri

    bucket, _ = parse_s3_uri(args.uri)
    with Store.open(bucket, cache_dir=settings.resolved_cache_dir()) as store:
        path = store.path
    for suffix in (".duckdb", ".duckdb.wal", ".lock"):
        path.with_suffix(suffix).unlink(missing_ok=True)
    return 0


def cmd_inventory(args: argparse.Namespace, settings: Settings) -> int:
    from s3fz.index.inventory import load_inventory, parse_manifest
    from s3fz.index.store import Store
    from s3fz.s3 import configure_duckdb_s3, make_store, parse_s3_uri

    bucket, prefix = parse_s3_uri(args.uri)
    mbucket, mkey = parse_s3_uri(args.manifest)
    text = bytes(obstore.get(make_store(mbucket, settings), mkey).bytes()).decode()
    manifest = parse_manifest(text)
    with Store.open(bucket, cache_dir=settings.resolved_cache_dir()) as store:
        configure_duckdb_s3(store.conn, settings)
        n = load_inventory(store, manifest, lambda k: f"s3://{mbucket}/{k}", prefix=prefix)
    sys.stdout.write(f"{n}\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    settings = Settings()
    log.configure(settings.log_level)
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args, settings))
    except S3fzError as e:
        logger.error(str(e))
        return e.exit_code
    except ValueError as e:
        logger.error(str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
