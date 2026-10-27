---
name: s3fz-pro-plugins
description: How to build the s3fz plugin boundary (src/s3fz/api.py, s3fz.plugins entry points) and the separate s3fz-pro package — naming-scheme packs, completeness rules, science preview, hand-off snippets, watch, du, permalinks, cost lens, versions, event-driven freshness, MCP server, Ed25519 licence check. Use whenever touching api.py, entry points, anything under s3fz_pro/, licence files, or any feature in the README v2 Pro table.
---

# s3fz plugin boundary and Pro

Two repos. `ejoliet/s3fz` (MIT) defines `s3fz.api` and the `s3fz.plugins` entry-point group. `ejoliet/s3fz-pro` (private) implements P1–P11 and a licence check. Core must be complete and pleasant without Pro.

## `s3fz.api` (stable, versioned: `API_VERSION = 1`)

Expose only what plugins need; keep signatures boring:

```python
register_tokens(specs: list[TokenSpec]); register_columns(cols: list[ColumnSpec])   # P1
register_source(source: ListingSource)                                                # P10, C4
register_preview(kind: str, fn: PreviewFn)                                            # P3
register_action(key: str, label: str, fn: ActionFn)                                   # P4, P6, P7, P8, P9
register_command(name: str, fn: CommandFn)                                            # watch, du, check, link, restore, mcp, events
Store (read-only query interface), Query, compile_query, Settings, EventBus (prefix_listed, object_seen)
```

Entry points: `[project.entry-points."s3fz.plugins"] pro = "s3fz_pro:register"`. Core calls `register(api)` for each plugin at startup, in a try/except that logs and continues; a broken plugin never prevents core from starting.

Core registers its own tokens, sources, previews, and actions through the same calls (Invariant 10 is tested by `tests/test_plugin_boundary.py` with a dummy plugin in `tests/fixtures/`).

## Licence (Pro)

- File `~/.config/s3fz/license.key`: base64 JSON payload `{sub, org, seats, exp, features}` + Ed25519 signature; public key embedded in `s3fz_pro`. Verify with `cryptography` at import; cache result per process.
- Missing/invalid/expired → each Pro command prints one stderr line and exits 5; `register()` still registers nothing Pro-only beyond a `pro status` command. Never degrade core.
- No network calls, no phone-home. Trial keys are ordinary keys with a 14-day `exp`.
- `s3fz pro status` prints holder, expiry, features; never the signature.

## Per-feature notes (what the agent would otherwise get wrong)

- **P1 naming-scheme packs**: YAML `schemes/<name>.yaml` with `match: regex` (named groups → columns) and `columns: {name: type}`. Applied as generated columns in a `objects_ext` side table keyed by `(bucket, key)`, populated in SQL via `regexp_extract` at ingest through an `on_ingest` hook, never per row in Python. Unmatched names get NULLs. Facet counts = `GROUP BY` on the side table.
- **P2 completeness rules**: rule kinds `sibling_required` (`when: product=cal`, `require: product in [uncal, cat]`, join on scheme columns minus product) and `set_cardinality` (`group_by: [visit]`, `expect: filter count = 6`). Output: missing items, `--tsv/--json`, exit 1 when gaps exist (for CI use).
- **P3 science preview**: FITS = ranged GET of 2,880-byte blocks until `END`; ASDF = ranged GET of the YAML header up to `...`; Parquet = footer via DuckDB `parquet_metadata` on the S3 URL. Thumbnails: downsample first image HDU via ranged reads of the first N rows only; encode PNG; emit Kitty graphics protocol or iTerm2 OSC 1337 by `$TERM`/`$TERM_PROGRAM`; otherwise text. Hard cap 1 MB transferred per preview.
- **P4 hand-off snippets**: templates in `s3fz_pro/snippets/*.py.j2`; print to stdout or copy; must run as-is against the moto demo bucket (test executes them).
- **P5 watch**: reuse the TTL crawler with TTL = `--interval` (default 30 s); emit on `object_seen` events filtered by tokens; `--notify` runs via `subprocess` with the key as `$1`; debounce duplicates by `(key, etag)`.
- **P6 du**: single `GROUP BY` queries over the index; treemap in preview as text blocks sized by share. Never walks S3.
- **P7 permalinks**: canonical form `s3fz://<bucket>/<prefix>?q=<urlencoded>&mode=deep&sort=mtime`; short id = first 8 chars of sha256, stored at `S3FZ_SHARED/links/<id>.json`; `s3fz open` resolves either form.
- **P8 cost lens**: bundled `prices.json` with a `dated` field shown in the UI; restore via `RestoreObject` with tier choice; this is the only write-like S3 call in the product and is Pro-only, confirmed interactively.
- **P9 versions**: `ListObjectVersions` into a separate `versions` table; `versions:on` token switches the query target; restore = `CopyObject` of the version onto the key, confirmed interactively. Both are the exceptions to core Invariant 1 and live only in Pro.
- **P10 event-driven freshness**: `s3fz events deploy` creates (via boto3, not CDK) an EventBridge rule on `Object Created`/`Object Deleted` for the bucket and a Lambda that appends `{op, key, size, mtime, etag}` JSONL to `S3FZ_SHARED/deltas/<bucket>/<yyyy-mm-dd-hh>.jsonl`. Sessions read deltas newer than each prefix's `listed_at` and apply them before trusting a shard. Print the IAM policy; do not create roles silently.
- **P11 MCP**: stdio MCP server using the official Python SDK; tools are thin wrappers over `s3fz.api`; all read-only; each tool returns ≤ 500 rows and the query it ran.

## Tests

Core: dummy plugin adds a token, a source, and a command; removing it leaves the suite green.
Pro: licence valid/expired/tampered; each P-feature's acceptance line from the README against the moto demo bucket; thumbnail test asserts fallback text when `$TERM` is `dumb`.
