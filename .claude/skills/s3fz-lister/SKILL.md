---
name: s3fz-lister
description: How to build and change the s3fz S3 crawler (src/s3fz/crawl/) — obstore async listing, Arrow batches, prefix fan-out, flat-prefix sharding, throttling, Inventory seeding. Use whenever touching crawl/, listing speed, ListObjectsV2, obstore, S3 Inventory, SlowDown/503 handling, credentials, or any "listing is slow" problem, even if the request doesn't name the module.
---

# s3fz lister

Listing is the only real bottleneck in s3fz. Everything here exists to keep as many `ListObjectsV2` calls in flight as S3 allows, while Python never touches individual keys.

## Facts that shape the design

- One call returns ≤ 1,000 keys, ~30–100 ms round trip. Pages inside one prefix are sequential (continuation token). Parallelism only comes from listing different prefixes at once.
- AWS documents 5,500 GET/HEAD req/s per partitioned prefix; LIST throughput is lower and not documented. Treat `SlowDown` (503) as the signal and adapt; never hard-code a rate.
- obstore wraps Rust `object_store`; `return_arrow=True` returns Arrow batches and removes Rust→Python per-object conversion. Requires `arro3-core`.

## Module layout

| File | Responsibility |
|------|----------------|
| `crawl/lister.py` | `list_dir(prefix) -> (common_prefixes, batch)` (delimiter) and `list_tree(prefix) -> AsyncIterator[RecordBatch]` (recursive) |
| `crawl/scheduler.py` | asyncio work queue, semaphore, BFS discovery, sharding, TTL, backoff, progress events |
| `index/inventory.py` | Inventory manifest → DuckDB (see `s3fz-index`) |

## Store construction

```python
from obstore.store import S3Store
from obstore.auth.boto3 import Boto3CredentialProvider  # AIDEV-NOTE: honours AWS_PROFILE, SSO, IRSA

store = S3Store(
    bucket,
    region=region,
    credential_provider=Boto3CredentialProvider(boto3.Session(profile_name=profile)),
    client_options={"timeout": "30s", "allow_http": endpoint.startswith("http://")},
    retry_config={"max_retries": 8, "retry_timeout": timedelta(seconds=120)},  # timedelta, not "120s": obstore 0.12 rejects strings here
    endpoint=endpoint or None,
)
```

`credential_provider`, `client_options`, `retry_config` and `endpoint` are the documented constructor names (obstore docs, checked 2026-10-26); confirm `RetryConfig` keys with `help(obstore.store.RetryConfig)`. `allow_http=True` is required for moto/MinIO on `http://`; path-style requests are the default (`virtual_hosted_style_request=False`). Results from `list` are not ordered; the index sorts.

## Crawl algorithm

1. **First paint.** `list_with_delimiter(prefix, return_arrow=True)` for the viewed prefix. Hand objects to the index; common prefixes become `prefixes` rows with `status='cold'`. This finishes before anything else is scheduled.
2. **Discovery (BFS).** Delimiter-list cold prefixes breadth-first up to depth 2 below the viewed prefix, or until ≥ 4 × parallelism leaf prefixes exist. This produces enough independent work units.
3. **Bulk.** For each work unit, `obs.list(store, prefix, chunk_size=1000, return_arrow=True)` and stream batches to the index writer. Mark `listing` → `warm` with `listed_at`, `object_count`.
4. **Flat-prefix sharding.** If a prefix has no common prefixes and its first page is full (1,000 keys), sample the next-character alphabet after the prefix from that page (hex, base36, base64, digits). Split into one shard per character (`prefix + c`) plus one catch-all job using `offset=` (start-after) past the last shard for characters not seen in the sample. Recurse once if a shard is still full and flat. Shards are listing jobs only; they never become `prefixes` rows.
5. **Concurrency.** One effective limit `S3FZ_PARALLELISM` (default 64) across all jobs. The writer is a single consumer of an `asyncio.Queue(maxsize=32)` of batches, so backpressure caps memory.
6. **Throttling.** On 503/`SlowDown`: halve the effective limit for 60 s (token counter, not a new semaphore), jittered exponential backoff, log once per minute.
7. **Errors.** `AccessDenied` → mark prefix `denied`, continue. Any other error after retries → `error` with message, continue. The crawler never raises into the UI.
8. **Priority.** The viewed prefix and its children always jump the queue. Navigating away does not cancel in-flight jobs; it reprioritises pending ones.

## Inventory seed

Prefer Inventory when available: zero API calls for millions of keys. Read `manifest.json`, take `files[].key`, hand paths to the `s3fz-index` loader. Mark loaded prefixes `source='inventory'`, `listed_at = manifest creationTimestamp`. Live-refresh only prefixes the user views.

## Threading model

asyncio for all S3 I/O, in one background thread with its own event loop. The UI/server thread never awaits S3. Progress goes out through a thread-safe queue → server → fzf `reload` POST (see `s3fz-fzf-ui`).

## Tests (moto server, no network)

- 2,500 keys in one prefix → 3 pages, all indexed once.
- Nested 3-level tree → BFS produces expected work units.
- Flat prefix of 5,000 hex-named keys → 16 shards + catch-all, no duplicates, no gaps (compare sorted key sets).
- `AccessDenied` on one prefix → others complete, prefix marked `denied`.
- Injected 503s → retries succeed, effective parallelism drops then recovers.

obstore speaks HTTP, so moto must run in server mode: session fixture starting `moto_server -p 5555`, with `S3FZ_ENDPOINT_URL=http://localhost:5555`.

## Perf check (Emmanuel runs)

```bash
py-spy top -- uv run s3fz index s3://<big-bucket>/ --wait
```
Python frames dominating CPU → a per-key loop crept in (Invariant 3). Idle CPU with low throughput → raise `S3FZ_PARALLELISM` or check sharding.
