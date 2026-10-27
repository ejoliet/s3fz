# Implementation notes

One line per non-obvious decision: what, why, alternative rejected. `DEVIATION:` marks departures from README/AGENTS.

- 2026-10-26 DEVIATION: Phase 0 and Phase 1 code written before a Gate-0 PASS is recorded. Gate-0 needs a real bucket, which the agent must not touch; `scripts/gate0.sh` is the manual proxy for Emmanuel. Nothing in Phase 0/1 depends on the Gate-0 outcome (index + inventory work even in the Inventory-only re-scope).
- 2026-10-26 Stack versions at scaffold: duckdb 2.0.0 (released 2026-10-21), obstore 0.12.0, pyarrow 26, moto 5.2.4, ruff 0.17, mypy 2.4. DuckDB 2.0 removed `fetch_arrow_table` on cursors and streams results; code uses `execute(...).fetchone()/fetchall()` and `sql(...).to_arrow_table()` only.
- 2026-10-26 Added `pytz` dependency: DuckDB 2.0 Python client needs it to materialise TIMESTAMPTZ values. Alternative rejected: storing naive UTC TIMESTAMP (README schema says TIMESTAMPTZ).
- 2026-10-26 Build backend `uv_build` (no setuptools/hatch): fits Invariant 8 (`uvx --from git+...`), no compiled step.
- 2026-10-26 `ext` requires a character before the dot: `.hidden` and `README` have `ext = NULL`; `a.b.c` -> `c` / `ext2 = b.c`. Skill lists these as test cases without expected values; chose Unix convention.
- 2026-10-26 Keys ending in `/` (folder-marker objects) are dropped at insert (`WHERE path NOT LIKE '%/'`): they would render as empty names in fzf. The prefix still appears via `prefixes`.
- 2026-10-26 `replace_prefix(kind='tree')` also upserts a warm `prefixes` row for every dir below the listed prefix (recursive CTE in SQL) with its own `max_mtime`, so "sort folders by modified" works right after one recursive listing. `object_count` of the listed prefix = whole listing; of dirs below = direct objects only.
- 2026-10-26 `max_mtime` rollup is one UPDATE per ancestor (handful of statements, SQL does the aggregation); a single statement over all ancestors was not worth the complexity.
- 2026-10-26 Index lock: `fcntl.flock` on `<bucket>.lock` holding the PID; a second DuckDB open failure (`IOException`) also maps to `IndexLocked` (exit 3). Read-only opens take no lock.
- 2026-10-26 Inventory loader embeds the `read_parquet/read_csv` SELECT as a subquery inside the insert; DuckDB cannot prepare `CREATE VIEW` with parameters. File list is bound as a list parameter. Parquet inventories use AWS snake_case column names (`last_modified_date`, `e_tag`, `storage_class`); CSV columns are positional (`column0`...) per `fileSchema`.
- 2026-10-26 obstore 0.12 `retry_config` takes `timedelta`, not duration strings (the `s3fz-lister` skill snippet shows strings; it is wrong for 0.12).
- 2026-10-26 Ruff `S608` ignored only for `src/s3fz/index/*`: those files interpolate code-owned SQL fragments (`SCOPE_SQL`, table names); user input never reaches them (Invariant 5 lives in `query/compiler.py`). Ruff also excludes `*.md` so `ruff format` leaves README code blocks alone. `PLC0415` ignored: CLI imports lazily on purpose (startup time).
- 2026-10-26 Perf probe (laptop, in-memory DuckDB): 1 M synthetic keys ingested via Arrow batches in 4.2 s; `name ILIKE '%f146%' ORDER BY last_modified DESC LIMIT 500` in scope: 20-30 ms; ancestor rollup 21 ms. No index needed.
- 2026-10-26 moto runs in server mode for tests (session fixture, free port): obstore and DuckDB httpfs speak HTTP, so in-process `mock_aws` does not apply.
