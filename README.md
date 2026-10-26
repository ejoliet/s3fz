# s3fz

> fzf-driven S3 explorer backed by a lazily built DuckDB index — `cd`, fuzzy-find, sort by mtime, and search by prefix/suffix/extension across buckets with millions of keys, from the terminal.

**Type**: A — Spec (agent implements from scratch)
**Author**: Emmanuel Joliet (`ejoliet`) · **Date**: 2026-10-25 · **Status**: Draft

---

## Purpose

**Problem.** Existing terminal S3 tools (`aws s3 ls`, `s5cmd ls`, `rclone`, `s3tui`) re-list the bucket on every navigation, cannot sort by last-modified across prefixes, and have no suffix/extension search. `ListObjectsV2` only filters by prefix; everything else means listing the whole subtree every time.

**Solution.** Keep a per-bucket DuckDB index of object metadata (key, size, mtime, etag, storage class). Fill it lazily: list the prefix you are looking at now, crawl deeper prefixes in the background, bulk-load from S3 Inventory when one exists. Drive the UI with `fzf`: directory mode is a fuzzy filter over the current prefix; deep-search mode pushes each keystroke to DuckDB as SQL (`ext:fits mtime>7d ^roman/ _cal.asdf$`) and shows the top hits sorted by mtime.

**Who benefits.** Anyone with large hierarchical buckets (pipeline outputs, mission archives) who needs "what changed under this prefix this week" or "all `*_cal.asdf` anywhere" without waiting for a full listing each time.

**Gap vs prior art** (checked 2026-10-25: PyPI `s3tui`, `s5cmd`, `rclone` incl. `ncdu` mode, `mountpoint-s3`, `s3fs-fuse`, S3 Inventory + Athena/DuckDB recipes, DuckDB `opendal_ls`).
None combine a persistent lazy index with an interactive fuzzy UI. `s3tui` filters only what is already listed. `s5cmd`/`rclone` are fast listers with no index. FUSE mounts give a filesystem but every `find` is a live listing. S3 Inventory + DuckDB gives the SQL half but is daily, needs bucket-owner config, and has no navigation UI. s3fz uses Inventory as an optional seed and adds the live lazy crawl plus the UI.

---

## Gate-0

| Item | Answer |
|------|--------|
| **Lowest-tech alternative** | `s5cmd ls -r s3://bucket/ \| duckdb -c "CREATE TABLE o AS SELECT ...; COPY o TO 'bucket.parquet'"` then `duckdb` SQL for mtime/suffix queries and `fzf` over the key column. Gets ~60%: correct answers, no interactivity, full re-list on every refresh, no per-prefix laziness. Documented as the fallback and used as the manual proxy below. |
| **Load-bearing assumption** | `ListObjectsV2` throughput on *your* real buckets is good enough that a subtree crawl finishes in minutes, not hours, when fanned out across first-level prefixes. 1,000 keys per call; S3 allows ~5,500 GET-class req/s per prefix. A flat namespace (millions of keys under one prefix) cannot be parallelised and would need Inventory. |
| **Manual proxy (≤ 3 h, no project code)** | 1. `time s5cmd ls -r s3://<largest-bucket>/ > keys.txt` 2. Load into DuckDB (`read_csv` with regex split) 3. Run: top 50 by mtime; `WHERE key LIKE '%_cal.asdf'`; `WHERE ext='fits' AND key LIKE 'roman/%'` 4. `cut -f4 keys.txt \| fzf` 5. `fzf --disabled --bind 'change:reload(duckdb bucket.duckdb -csv -c "SELECT key FROM o WHERE key ILIKE ''%{q}%'' ORDER BY mtime DESC LIMIT 200")'` |
| **Pass / fail** | PASS if: full listing of the largest bucket < 10 min with `s5cmd` (obstore target after build: 10 M keys in 1–3 min); each DuckDB query < 500 ms on ≥ 1 M keys; the `change:reload` loop feels interactive (< 200 ms per keystroke). FAIL on any → re-scope to Inventory-only (no live crawl) or stop. |
| **Repo decision** | New repo `ejoliet/s3fz`. Not a feature of `awsnap` (account-wide snapshot, HTML output, different cadence). Could later feed `awsnap` an object-level Parquet. |
| **Maintenance** | No hosted component, no protocol others depend on, no keys to rotate. Depends on `boto3`, `duckdb`, `fzf` — all stable APIs. Can sit a year unmaintained. |

> ⚠️ Run the manual proxy first. Record the timing numbers in this section before Phase 0 of the build order.

---

## Architecture

```
┌──────────── s3fz (single Python process, owns the DuckDB file) ────────────┐
│                                                                             │
│  fzf (subprocess, --listen)  <── push reload ──  Query engine (DuckDB SQL)  │
│        │ keystrokes / keys                              ▲                   │
│        └── unix socket ──> Request router ──────────────┘                   │
│                                 │                                           │
│                          Crawl scheduler (thread pool, per-prefix TTL)      │
│                                 │                                           │
│              ┌──────────────────┴───────────────────┐                       │
│         ListObjectsV2 (boto3)                S3 Inventory loader            │
│         delimiter list = "ls"                (read_parquet / read_csv)      │
│         recursive list = "find"                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                  │
                       ~/.cache/s3fz/<bucket>.duckdb
```

Data flow:

1. `s3fz s3://bucket/prefix/` opens (or creates) the bucket index and spawns `fzf --listen`.
2. The current prefix is listed with a delimiter (one or a few calls) and replaced in the index (delete-then-insert for that prefix level). fzf shows children immediately, newest first.
3. A background scheduler crawls child prefixes recursively with bounded parallelism, updating `prefixes.status` cold → listing → warm. The status bar shows coverage (`warm 12/40 prefixes · 1.2M objects · oldest 3 h`).
4. In deep-search mode, each keystroke triggers fzf `reload-sync` running a `curl --unix-socket` client against the s3fz query server (no Python startup per keystroke); fzf kills the previous reload when a new key arrives. When the crawler finishes a prefix, s3fz POSTs `reload(...)` to fzf's own socket (`$FZF_SOCK`) so results refresh without typing. Prefixes still cold are shown as "not yet indexed" in the preview.
5. If `--inventory` is given (or auto-detected from a bucket's inventory configuration), the manifest's Parquet/CSV files are bulk-loaded first, then marked `warm` with `listed_at = inventory date`.

> 💡 **Single DB owner.** DuckDB allows one writing process per file. fzf's `reload` would spawn fresh processes that cannot open the file, so all queries are served by the main process over a Unix-socket HTTP server, and fzf reaches it with `curl --unix-socket`. Do not spawn `duckdb` or `python` per keystroke.

### Index schema

```sql
CREATE TABLE objects (
  bucket        VARCHAR NOT NULL,
  key           VARCHAR NOT NULL,
  dir           VARCHAR NOT NULL,   -- key up to and including last '/'
  name          VARCHAR NOT NULL,   -- after last '/'
  ext           VARCHAR,            -- lowercased, after last '.', NULL if none
  ext2          VARCHAR,            -- last two extensions, e.g. 'fits.gz'
  size          BIGINT  NOT NULL,
  last_modified TIMESTAMPTZ NOT NULL,
  rkey          VARCHAR NOT NULL,   -- reverse(key): suffix search becomes a prefix LIKE
  etag          VARCHAR,
  storage_class VARCHAR,
  seen_at       TIMESTAMPTZ NOT NULL
);
-- No PRIMARY KEY and no ART indexes: they slow 10 M-row bulk loads and add memory.
-- Rows are inserted sorted by key. Prefix scope is written as a range (`key >= p AND key < p || chr(255)`),
-- which zone maps always prune; `starts_with` alone may not be pushed down. Other predicates
-- are vectorized scans (tens of ms at 10 M rows). Add an index only if a perf test fails.
-- Refresh = delete the listed prefix's rows, then insert the new listing (also removes
-- objects deleted in S3, which an upsert would never do).

CREATE TABLE prefixes (
  bucket       VARCHAR NOT NULL,
  prefix       VARCHAR NOT NULL,   -- '' = root
  status       VARCHAR NOT NULL,   -- cold | listing | warm | denied | error
  listed_at    TIMESTAMPTZ,
  object_count BIGINT,
  max_mtime    TIMESTAMPTZ,        -- newest object anywhere below
  source       VARCHAR,            -- live | inventory
  PRIMARY KEY (bucket, prefix)
);
```

`max_mtime` is what makes "sort directories by modified" possible: it is rolled up from children after every crawl.

### Performance design (decided)

| Hot path | Decision |
|----------|----------|
| Listing source order | 1. S3 Inventory manifest → `read_parquet` 2. `obstore` async recursive list, Arrow batches 3. `s5cmd` escape hatch |
| Parallelism | Listing is sequential inside a prefix; fan out across prefixes. Delimiter-list root, BFS-discover subprefixes, recursive-list them under a semaphore of 64. |
| Flat prefixes | Shard by next character: `prefix/0`…`prefix/f` listed concurrently (16/36/64 shards by alphabet detected from a sample page). No Inventory required. |
| Rate limits | Adaptive backoff on `SlowDown`; the documented 5,500 req/s per prefix figure is for GET/HEAD; LIST throughput is lower and undocumented, so the crawler measures it (Gate-0) rather than assuming. Target: 10 M keys in 1–3 min on hierarchical buckets. |
| Ingest | Arrow RecordBatch → DuckDB `INSERT ... FROM arrow_batch` zero-copy; batches sorted by key so zone maps serve prefix scans. |
| Suffix search | `rkey = reverse(key)` column; suffix query is a prefix `LIKE` on `rkey`. |
| Substring / fuzzy | Deep mode runs fzf `--disabled`: SQL is authoritative. Substring = `ILIKE '%x%'`; fuzzy `~abc` = fzf-style subsequence `ILIKE '%a%b%c%'`; both `ORDER BY last_modified DESC LIMIT 500`. No similarity functions. |
| Refresh | Per-prefix TTL; re-list only the viewed prefix; Inventory deltas via anti-join. Never full re-crawl on open. |
| Language | Python is glue between obstore (Rust), DuckDB (C++), fzf (Go). No per-key Python loops anywhere. |


### Query language

Space-separated tokens, all ANDed. Compiled to one SQL statement.

| Token | Meaning | SQL |
|-------|---------|-----|
| `word` | substring of `name`, case-insensitive | `name ILIKE '%word%'` |
| `r001101*` | glob on `name` (any token with `*` or `?`) | `name LIKE 'r001101%'` (index on `name`) |
| `*f146*` | glob on `name` | `name ILIKE '%f146%'` |
| `roman/l2/*/r0011*` | glob on `key` (token contains `/`) | `key GLOB 'roman/l2/*/r0011*'` |
| `^roman/l2` | key prefix (shorthand) | `key LIKE 'roman/l2%'` |
| `_cal.asdf$` | key suffix | `rkey LIKE 'fdsa.lac_%'` (reversed, index-friendly) |
| `ext:fits,asdf` | extension in set | `ext IN ('fits','asdf')` |
| `mtime>7d` / `mtime<2026-10-01` | modified relative/absolute | `last_modified > now() - INTERVAL 7 DAY` |
| `size>1g` / `size<10k` | size bounds | `size > 1073741824` |
| `~f146` | fzf-style subsequence on `name` | `name ILIKE '%f%1%4%6%'` |
| `sort:name|mtime|size` | ordering (default `mtime` desc) | `ORDER BY ...` |

Glob rules: `*` and `?` are the only metacharacters; `_` and `%` in user input are escaped before `LIKE`. A glob with a leading literal (`r001101*`) compiles to an index-friendly prefix `LIKE`; a leading `*` is a scan. Everything is parameterised. No string concatenation of user input into SQL.

fzf runs with `--exact` so a plain query in directory mode is a substring match, not fuzzy (`'` toggles are therefore unnecessary; `!` still negates).

---

## Recommended Stack

GitHub star counts were not retrievable at generation time (unauthenticated API rate limit). Pull them yourself:

```bash
for r in junegunn/fzf duckdb/duckdb boto/boto3 Textualize/textual ratatui/ratatui peak/s5cmd; do
  curl -s -H "Authorization: Bearer $(gh auth token)" "https://api.github.com/repos/$r" \
    | jq -r '[.full_name,.stargazers_count,.pushed_at[:10],.open_issues_count] | @tsv'; done
```

| Layer | Chosen | Why chosen | Rejected |
|-------|--------|------------|----------|
| UI | `fzf` (external binary, `--listen`, `--preview`, `--bind`) | You asked for fzf semantics; fuzzy matching, multi-select, preview and key bindings are free; one process to maintain | `Textual` (richer, 5–10× more UI code, v2 candidate); `ratatui`/`bubbletea` (Rust/Go, breaks the `uvx` install contract); `skim` (fewer users, no `--listen`) |
| Index | `duckdb` Python package, single file per bucket | Columnar, Parquet-native (Inventory load is one `read_parquet`), window/rollup for `max_mtime`, zero server | SQLite (no native Parquet, slower sorts on 10 M rows); Postgres (server) |
| S3 client | `obstore` (Rust `object_store`, async, `return_arrow=True`) | Rust listing, Arrow batches straight into DuckDB with no per-key Python work; honours AWS credential chain; `uvx`-installable | `boto3` + threads (per-key Python dicts, 5–10× slower ingest); `aioboto3` (same overhead); `s5cmd` shell-out (kept as optional `--lister s5cmd` escape hatch) |
| Inventory seed | DuckDB `read_parquet` / `read_csv` over the manifest's `files[]` | Bulk-load 10 M keys in seconds | Athena (needs Glue table, per-query cost) |
| Packaging | `uv`, `pyproject.toml`, `[project.scripts] s3fz` | `uvx --from git+https://github.com/ejoliet/s3fz s3fz` | pipx |
| Tests | `pytest`, `moto` for S3, `pytest-timeout` | No real AWS calls in CI | — |

Community signals to confirm during Phase 1 of the build: fzf ≥ 0.66 Unix-socket `--listen`, DuckDB write-concurrency semantics (duckdb.org "Concurrency" page), boto3 adaptive retry behaviour under `SlowDown`.

---

## Repository Layout

```
s3fz/
├── pyproject.toml            # uv; [project.scripts] s3fz = "s3fz.cli:main"
├── README.md                 # this file
├── AGENTS.md                 # agent invariants; CLAUDE.md imports it
├── CLAUDE.md                 # `@AGENTS.md`
├── .claude/skills/           # s3fz-lister, s3fz-index, s3fz-query, s3fz-fzf-ui, s3fz-tech-writer
├── docs/                     # user docs (s3fz-tech-writer); tables generated from code
├── scripts/                  # gen_docs.py, demo_bucket.py, capture_examples.sh
├── RELEASE_NOTES.md
├── src/s3fz/
│   ├── cli.py                # argparse; subcommands: open, index, query, inventory, cache, completion
│   ├── config.py             # env vars → Settings (pydantic)
│   ├── index/
│   │   ├── schema.py         # DDL, migrations
│   │   ├── store.py          # replace_prefix, mark_prefix, rollup_max_mtime
│   │   └── inventory.py      # manifest.json → read_parquet/read_csv → objects
│   ├── crawl/
│   │   ├── lister.py         # ListObjectsV2 wrapper (delimiter + recursive), paginator
│   │   └── scheduler.py      # thread pool, TTL, per-prefix locks, backoff
│   ├── shared/
│   │   ├── manifest.py       # GET/merge/CAS-PUT manifest, rebuild from listing
│   │   ├── shards.py         # write/read Parquet shards via DuckDB httpfs
│   │   └── history.py        # saved searches + team history JSONL
│   ├── query/
│   │   ├── tokens.py         # TokenSpec table: grammar + docs source of truth
│   │   ├── parser.py         # token grammar → AST
│   │   └── compiler.py       # AST → parameterised SQL
│   ├── ui/
│   │   ├── keymap.py         # key bindings: UI + docs source of truth
│   │   ├── fzf.py            # spawn fzf --listen, bindings, POST reload via $FZF_SOCK
│   │   ├── server.py         # HTTP-over-Unix-socket query server (curl client)
│   │   └── preview.py        # object/prefix preview text (HEAD, tags, children)
│   └── actions.py            # download, copy URI, presign, open in $PAGER
└── tests/
    ├── test_docs_sync.py     # generated doc tables match code
    ├── test_parser.py        # token grammar
    ├── test_compiler.py      # SQL output, parameter binding
    ├── test_store.py         # replace_prefix idempotence + deletions, rollup
    ├── test_lister.py        # moto buckets, pagination, AccessDenied
    └── test_inventory.py     # manifest parsing, Parquet + CSV variants
```

---

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.11+ | |
| uv | latest | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| fzf | ≥ 0.66 | `brew install fzf`; needed for `--listen /path.sock` (Unix socket) and `$FZF_SOCK`; `--accept-nth` needs >= 0.60 |
| curl | any | fzf bindings call the query server with `curl --unix-socket` |
| AWS credentials | — | profile, SSO, env vars or IRSA; `s3:ListBucket`, `s3:GetObject` (download/preview), `s3:GetBucketInventoryConfiguration` (auto-detect inventory, optional) |

---

## Quick Start

```bash
# run straight from GitHub
uvx --from git+https://github.com/ejoliet/s3fz s3fz s3://my-bucket/roman/

# or install once
uv tool install git+https://github.com/ejoliet/s3fz
s3fz s3://my-bucket/                  # browse root, lazy crawl starts
s3fz s3://my-bucket/ --inventory s3://my-inventory-bucket/my-bucket/daily/   # seed first
s3fz query s3://my-bucket/ 'ext:asdf mtime>7d _cal$' --limit 50 --json        # headless
s3fz index s3://my-bucket/roman/l2/ --depth 3 --wait                          # warm a subtree
s3fz cache ls / rm s3://my-bucket/                                            # manage index files
```

### Keys inside fzf

| Key | Action |
|-----|--------|
| `Enter` | prefix → `cd` into it; object → print `s3://` URI and exit |
| `Backspace` on empty query | parent prefix (fzf `backward-eof`) |
| `Ctrl-F` | toggle directory mode ↔ deep-search mode (whole subtree, SQL tokens) |
| `Tab` / `Shift-Tab` | multi-select (fzf default) |
| `Ctrl-S` | cycle sort: mtime ↓ (default) → name → size ↓ |
| `Ctrl-R` | re-list current prefix now (ignore TTL) |
| `Ctrl-Y` | copy `s3://` URI(s) to clipboard (`pbcopy` / `xclip`) |
| `Ctrl-D` | download selected to `$PWD` (never overwrites; adds `.1`, `.2`) |
| `Alt-P` | print presigned URL(s), 15 min, and exit |
| `Ctrl-/` | toggle preview (HEAD metadata, tags, first 2 KB for text types) |
| `Ctrl-H` | team search history and saved searches (with `S3FZ_SHARED`) |
| `Alt-D` | diff current prefix against the previous cached snapshot |
| `Ctrl-O` | Pro: print analysis snippet (astropy / lsdb / s5cmd manifest) for the selection |
| `Alt-U` | Pro: `du` view of current prefix |
| `Alt-L` | Pro: copy a permalink for the current view and query |
| `Alt-I` | show index coverage for current subtree |
| `Esc` | quit |

---

## Configuration Reference

| Env var | Type | Default | Required | Description |
|---------|------|---------|----------|-------------|
| `S3FZ_CACHE_DIR` | path | `~/.cache/s3fz` | no | Index files location |
| `AWS_PROFILE` / `AWS_REGION` | string | boto3 defaults | no | Standard AWS resolution |
| `S3FZ_ENDPOINT_URL` | URL | unset | no | MinIO / non-AWS S3 |
| `S3FZ_TTL` | duration | `1h` | no | Re-list a warm prefix after this age when viewed |
| `S3FZ_PARALLELISM` | int | `16` | no | Concurrent `ListObjectsV2` calls in the crawler |
| `S3FZ_MAX_RESULTS` | int | `500` | no | Rows pushed to fzf per deep-search query |
| `S3FZ_CRAWL_DEPTH` | int | `unlimited` | no | Background crawl depth below the viewed prefix |
| `S3FZ_INVENTORY_AUTODETECT` | bool | `true` | no | Call `GetBucketInventoryConfiguration` on open |
| `S3FZ_FZF_BIN` | path | `fzf` on `$PATH` | no | Override fzf binary |
| `S3FZ_LOG_LEVEL` | string | `WARNING` | no | Structured JSON logs to stderr |

---

## API / Interface Contract

### CLI

```
s3fz [open] S3URI [--inventory S3URI] [--no-crawl] [--sort mtime|name|size]
s3fz query S3URI TOKENS... [--limit N] [--json|--tsv] [--live]   # --live lists cold prefixes before answering
s3fz index S3URI [--depth N] [--wait] [--force]
s3fz inventory S3URI --from MANIFEST_S3URI
s3fz cache (ls | rm S3URI | path S3URI)
s3fz completion bash            # prints a completion script; `s3fz s3://bucket/pre<Tab>` completes prefixes from the index
s3fz widget bash                # key binding (Ctrl-X Ctrl-S) opens s3fz and inserts the chosen s3:// path at the cursor; bash >= 4 uses `bind -x`, Apple bash 3.2 uses fzf's readline-macro technique
s3fz diff S3URI --since 2d | --between T1 T2 | --against S3URI2   # added / removed / modified from cache snapshots
s3fz save NAME 'TOKENS...' / s3fz go NAME                          # saved searches and scope aliases
```

Pro (`s3fz-pro` package, see Open-core split):

```
s3fz watch S3URI [TOKENS...] [--notify cmd]
s3fz du S3URI [--by prefix|ext|day] [--depth N]
s3fz check S3URI --rules rules.yaml          # completeness rules
s3fz link 'TOKENS...'  /  s3fz open s3fz://…  # permalinks
s3fz restore S3URI...  /  versions:on token   # Glacier restore, versions and delete markers
s3fz mcp                                       # MCP server over the same engine
s3fz events deploy --bucket B                  # Lambda + EventBridge delta writer for S3FZ_SHARED
```

Exit codes: `0` ok · `1` usage · `2` AWS auth/permission · `3` index locked by another process · `4` fzf missing · `5` Pro licence missing/invalid.

### Python surface (for scripting and tests)

```python
from s3fz.index.store import Store
from s3fz.query.parser import parse
from s3fz.query.compiler import compile_query

store = Store.open("my-bucket")                     # ~/.cache/s3fz/my-bucket.duckdb
sql, params = compile_query(parse("ext:asdf mtime>7d _cal$"), scope="roman/", limit=50)
rows = store.conn.execute(sql, params).fetchall()   # (key, size, last_modified, dir)
```

### Unix socket protocol (fzf ↔ main process)

Newline-delimited JSON on `$S3FZ_CACHE_DIR/<bucket>.sock`.
Requests: `{"op":"query","mode":"dir|deep","prefix":"...","q":"..."}`, `{"op":"preview","key":"..."}`, `{"op":"action","name":"download","keys":[...]}`.
Responses: `{"ok":true,"rows":[...]}` or `{"ok":false,"error":"...","code":"AccessDenied"}`.

---

## Error Handling

| Error class | Trigger | Behaviour |
|-------------|---------|-----------|
| `PrefixDenied` | `AccessDenied` on a prefix | Mark `prefixes.status='denied'`, continue crawl, show `🔒` in UI |
| `Throttled` | `SlowDown` / 503 | botocore adaptive retry; scheduler halves parallelism for 60 s, logs once |
| `IndexLocked` | second `s3fz` on same bucket | Exit 3 with the PID holding the lock; `s3fz query --read-only` opens `READ_ONLY` if no writer |
| `InventoryStale` | manifest date > 48 h | Load anyway, show age in status bar, schedule live refresh of viewed prefixes |
| `FzfMissing` | binary not found | Exit 4 with install hint |
| `SchemaMismatch` | older index file | Auto-migrate; if impossible, rename to `*.bak` and rebuild |

Crawler never aborts on a single prefix failure. UI never blocks on the crawler.

---

## Testing

```bash
uv sync --group dev
uv run pytest -q                 # moto-backed; no network
uv run pytest -m perf            # optional: 1 M synthetic keys, asserts query < 500 ms
uv run ruff check . && uv run mypy --strict src
```

| Suite | Covers |
|-------|--------|
| `test_parser` | every token, globs (`r001101*`, `*f146*`, `a/*/b*`), bad tokens, `_`/`%` escaping |
| `test_compiler` | SQL shape, parameters only (no inlined input), sort/limit |
| `test_store` | replace_prefix idempotence, deleted objects disappear, `max_mtime` rollup, TTL logic, migration |
| `test_lister` | pagination over 2,500 keys, delimiter vs recursive, `AccessDenied` isolation |
| `test_inventory` | Parquet and CSV manifests, gzip, column mapping |
| manual | `fzf` integration — run `s3fz` against a moto server: `uv run moto_server & s3fz s3://t/ --endpoint-url http://localhost:5000` |

---

## Shared cache (cross-machine, cross-user)

Set `S3FZ_SHARED=s3://team-cache/s3fz/`. Every session pulls per-prefix Parquet shards and a manifest from there before listing anything, and publishes the prefixes it listed itself. Second laptop, second person, same bucket: one GET for the manifest, then shards on demand, instead of thousands of `ListObjectsV2` calls. Shards are immutable and last-writer-wins per prefix; the manifest merges with S3 conditional writes (`If-Match`). Saved searches and team search history live beside it. Design and rules: `.claude/skills/s3fz-shared-cache/SKILL.md`.

| Env var | Default | Description |
|---------|---------|-------------|
| `S3FZ_SHARED` | unset | `s3://…/` location of shards, manifest, saved searches |
| `S3FZ_SHARED_PUBLISH` | `auto` | publish listings there: `auto` if writable, `on`, `off` |
| `S3FZ_SHARED_MIN_DELTA` | `5m` | skip publishing when the existing shard is this fresh |
| `S3FZ_SHARED_HISTORY` | `1` | append deep searches to team history |
| `S3FZ_SHARED_HINTS` | `0` | record prefix opens to order prefetch |

## Non-Goals (v1)

- No writes: no delete, copy, move, or upload. Download only.
- No FUSE mount. Not a filesystem; an index plus a finder.
- No object-content grep. Header/schema peek is a v2 Pro feature, not grep.
- No custom TUI framework. fzf is the UI until a concrete limit is hit.
- No multi-bucket union search (one bucket per session; `s3fz query` can be looped in shell).
- No hosted index server. Sharing happens only through `S3FZ_SHARED` objects in S3.
- No S3 versions / delete markers in the core index (v2 Pro).
- No Windows support.

---

## v2 features (ordered by expected daily use)

Core (free) additions beyond v1:

| # | Feature | Acceptance |
|---|---------|------------|
| C1 | Shell widget (`s3fz widget bash`): key binding inserts the selected `s3://` path into the current command line; `bind -x` with `READLINE_LINE` on bash >= 4, readline-macro fallback on bash 3.2 (copy fzf's `key-bindings.bash` approach) | Typing `aws s3 cp ` then the key, picking a file, yields the full command line with the path inserted at the cursor |
| C2 | `s3fz diff`: added/removed/modified between two cached snapshots of a prefix (`--since`, `--between`), or between two prefixes (`--against`) | Diff of the moto demo bucket across two listings reports exactly the injected changes; output `--tsv`/`--json` |
| C3 | Saved searches and scope aliases (`s3fz save`, `s3fz go`), local and shared | Round-trips through `S3FZ_SHARED`; `Ctrl-H` lists them |
| C4 | S3 Metadata as a listing source for buckets you own: the live `inventory` table (full state, refreshed within ~1 h) and the `journal` table (change events) are AWS-managed Iceberg tables in the `aws-s3` table bucket, readable by DuckDB through the S3 Tables Iceberg REST catalog. Source priority becomes: S3 Metadata -> S3 Inventory -> live crawl. Journal rows applied since each prefix's `listed_at` give freshness without listing | Confirm DuckDB `iceberg` + S3 Tables catalog read works in the account before building; then zero `ListObjectsV2` calls on a bucket with S3 Metadata enabled |

Pro additions (separate `s3fz-pro` package, licence-gated):

| # | Feature | Acceptance |
|---|---------|------------|
| P1 | Naming-scheme packs: per-bucket YAML regex → columns (visit, exposure, filter, product); tokens `filter:f146 product:cal`; facet counts in header. Ships Roman, SPHEREx, Euclid, ZTF | `filter:f146 product:cal` on the demo bucket returns exactly the matching rows; unmatched names stay searchable |
| P2 | Completeness rules (`s3fz check --rules`): sibling-required and set-cardinality rules | Rule file with "every `_cal` needs `_uncal`" reports the injected gap only |
| P3 | Science preview: FITS primary header, ASDF tree, Parquet schema and row count via ranged GET; inline thumbnail in Kitty / iTerm2 / WezTerm (graceful text fallback) | Preview of a 2 GB FITS transfers < 64 KB; thumbnail appears in a supported terminal, text elsewhere |
| P4 | Hand-off snippets (`Ctrl-O`): astropy `fits.open` with fsspec, `lsdb.read_hats`, `s5cmd run` manifest, Airflow param JSON | Each snippet runs unmodified against the moto demo bucket |
| P5 | Watch mode (`s3fz watch`): tail a prefix with optional token filter; `--notify cmd` runs per new object | New object appears within one TTL cycle; `--notify` invoked once per object |
| P6 | `du` view (`Alt-U`, `s3fz du`): size and count by child prefix, extension, or day; treemap in preview | Totals equal `SUM(size)` from the index; sort by size |
| P7 | Permalinks (`s3fz link`, `Alt-L`): `s3fz://bucket/prefix?q=…` plus a short id stored under `S3FZ_SHARED/links/` | Opening a link restores scope, mode, query, sort |
| P8 | Cost lens: Glacier / Deep Archive flagged in rows; selection shows retrieval cost estimate and restore time; `s3fz restore` | Estimate uses a bundled, dated price table; the date is shown |
| P9 | Versions and delete markers: `versions:on` scope token; restore a version from the UI | Deleted object appears with its delete time; restore reinstates it in moto |
| P10 | Event-driven freshness for buckets where S3 Metadata cannot be enabled (non-AWS stores, buckets you cannot configure): Lambda + EventBridge append JSONL deltas to `S3FZ_SHARED/deltas/`. On AWS buckets you own, C4's journal table replaces this; build P10 only if a paying user needs it | After deploy, a new object is visible in a fresh session with zero `ListObjectsV2` calls on its prefix |
| P11 | MCP server (`s3fz mcp`): tools `search_objects`, `diff`, `du`, `preview_header`, `check` over the same engine and shared cache | Claude Code lists and searches the moto demo bucket through the server; tools are read-only |

## Open-core split

Pattern: open core plus an offline Ed25519 licence check, sold through Lemon Squeezy, same as the rest of the portfolio.

| | Core `s3fz` (MIT) | Pro `s3fz-pro` |
|---|---|---|
| Contents | browse, search, local index, shared cache, shell widget, diff, saved searches, completion | P1–P11 |
| Packaging | `uvx --from git+https://github.com/ejoliet/s3fz s3fz` | separate private repo; `uv tool install s3fz-pro` from a Lemon Squeezy-delivered wheel or private index; registers via the `s3fz.plugins` entry-point group |
| Licence check | none | Ed25519-signed licence file at `~/.config/s3fz/license.key`; verified offline at import; expiry and seat count in the payload; core runs unaffected when absent |
| Pricing (initial) | free | $8 per user per month or $120 per org per month, yearly; 14-day trial key |

Plugin boundary (so the split is real, not cosmetic):

- Core defines the extension points: `s3fz.plugins` entry points for `tokens` (extra `TokenSpec`s and columns), `sources` (listing sources), `preview`, `actions`, `commands`. Core ships its own features through the same mechanism, so the boundary is exercised in CI.
- Core never imports from `s3fz_pro`. Pro imports only from `s3fz.api` (a stable, versioned module).
- A missing or invalid licence disables Pro commands with one stderr line and exit 5; it never degrades core.
- Build order: C1–C4 ship with core v1.0; P1, P3, P4 first in Pro (highest daily use, pure client code); P10 last (infrastructure).

## Open Questions

- [ ] Name: `s3fz` — check PyPI and GitHub availability; alternatives `bucketfz`, `lazybucket`.
- [ ] Gate-0 numbers: listing time of the largest real bucket, flat-namespace share.
- [ ] Inventory auto-detect requires `GetBucketInventoryConfiguration`, which many roles lack; keep it best-effort.
- [ ] Accelerator: shell-out to `s5cmd ls -r` when present and the subtree is cold and large (> N prefixes)?
- [ ] v2: Textual TUI with two-pane (tree + results) if fzf key-binding limits bite.
- [ ] C4: confirm DuckDB `iceberg` extension reads the `aws-s3` table bucket via the S3 Tables REST catalog with your role; note S3 Metadata charges per change event and per object for inventory backfill.
- [ ] Pro distribution: private PyPI index vs wheel download link from Lemon Squeezy.

---

## Agent Build Instructions

> Implement end-to-end using only this README, `AGENTS.md`, and the skills in `.claude/skills/`. Resolve Open Questions first. Do not spend tokens on visual review, browser checks, GitHub pushes, or heavy installs; print the exact commands for Emmanuel to run instead.

### Build Order

| Phase | Deliverable | Done when |
|-------|-------------|-----------|
| G0 | Gate-0 manual proxy (Emmanuel runs; agent records result) | Pass/fail line met, numbers recorded in Gate-0 |
| 0 | Scaffold: `pyproject.toml`, `src/s3fz`, `AGENTS.md`, ruff/mypy config | `uv run ruff check . && uv run mypy --strict src` pass |
| 1 | `index/` schema + store + inventory loader | `test_store`, `test_inventory` pass |
| 2 | `crawl/` lister + scheduler | `test_lister` passes; `s3fz index --wait` warms a moto bucket |
| 3 | `query/` parser + compiler | `test_parser`, `test_compiler` pass; `s3fz query --json` works headless |
| 4 | `ui/` fzf spawn, socket, preview, actions | Manual session against moto server works per Keys table |
| 4b | `shared/` manifest, shards, history | moto tests in `s3fz-shared-cache` skill pass |
| 4c | `s3fz.api` + `s3fz.plugins` entry points; core features registered through them | A dummy plugin in tests adds a token and a command |
| 4d | C1 widget, C2 diff, C3 saved searches | Acceptance lines in v2 table |
| 5 | Packaging: `uvx --from git+... s3fz --help` works from a clean machine | Emmanuel runs the command; exit 0 |
| 6 | `s3fz-pro` repo: licence check, P1, P3, P4 | Acceptance lines; core test suite unchanged |
| 7 | Pro P2, P5–P9, P11; then P10 | Acceptance lines |

### Constraints

- Python 3.11+; typed signatures everywhere; `ruff` + `mypy --strict` clean.
- All SQL parameterised. Never format user tokens into SQL strings.
- One process owns the DuckDB file. Lock file with PID next to the index.
- Secrets only from the AWS credential chain. Never log keys, presigned URLs, or credentials.
- Tests never hit real AWS (`moto`).
- `AIDEV-` grep-able comments for non-obvious decisions (e.g. the single-owner rule). No comments restating obvious code.
- Structured JSON logs to stderr; stdout reserved for results (so `s3fz query` pipes cleanly).

### Acceptance Criteria

- [ ] Gate-0 result recorded with timings before Phase 0 started
- [ ] `uv run pytest` passes, coverage ≥ 80%
- [ ] `uv run ruff check .` and `mypy --strict` pass
- [ ] `uvx --from git+https://github.com/ejoliet/s3fz s3fz --help` exits 0 on a clean macOS machine
- [ ] Directory mode first paint < 1 s on a cold prefix with ≤ 1,000 children
- [ ] Deep-search keystroke → results < 200 ms on a 1 M-row warm index (perf test)
- [ ] Every key in the Keys table works against a moto bucket
- [ ] All Open Questions resolved or moved to a `v2` section

---

## Next Steps

1. Run the Gate-0 manual proxy on your largest real bucket; paste timings into Gate-0.
2. Confirm or override the Recommended Stack table (fzf vs Textual is the main call).
3. Check name availability on PyPI/GitHub.
4. Create `ejoliet/s3fz`, drop this README in, generate `AGENTS.md` with `ai-agent-md-architect`, hand to the build agent.
