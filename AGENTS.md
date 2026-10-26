<!-- AGENTS.md — agent guidance for s3fz
     Last reviewed: 2026-10-25  Owner: Emmanuel Joliet (ejoliet)
     AIDEV-NOTE: keep ≤ 200 lines; overflow goes into .claude/skills/ -->

# s3fz

Terminal S3 explorer: fzf UI over a lazily built per-bucket DuckDB index of object metadata. Spec: `README.md`. Owner: Emmanuel.

---

## 🛑 Project Invariants (DO NOT VIOLATE)

<!-- AIDEV-INVARIANT: rule + reason + blast radius. -->

1. **Read-only against S3.** Only `ListObjectsV2`, `HeadObject`, `GetObject`, `GetObjectTagging`, `GetBucketInventoryConfiguration`, presign. Never put/copy/delete.
   Blast: data loss in mission buckets (Roman, SPHEREx); tool becomes unsafe to hand to colleagues.
2. **One process owns the DuckDB file.** fzf reaches data only through the s3fz query server (`curl --unix-socket`). Never spawn `duckdb` or `python` per keystroke.
   Blast: `IO Error: Could not set lock`, frozen UI, 40 ms+ added to every keystroke.
3. **No per-key Python loops on hot paths.** Listing → Arrow batches (obstore `return_arrow=True`) → DuckDB. Python only moves batches.
   Blast: ingest drops 5–10×; 10 M-key buckets take 20+ min instead of 1–3.
4. **Refresh = replace the listed prefix, never upsert.** Delete that prefix's rows, insert the new listing, in one transaction.
   Blast: objects deleted in S3 stay in the index forever; users download 404s and trust nothing.
5. **All SQL parameterised; user `_` and `%` escaped before `LIKE`.** The query compiler is the only SQL builder for user input.
   Blast: wrong results on names with `_` (very common in mission filenames) and SQL injection via the query box.
6. **No secrets in logs, stdout, index, or tests.** No keys, session tokens, presigned URLs in logs. Credentials come only from the AWS chain.
   Blast: credential leak in shared terminals, CI logs, or bug reports.
7. **stdout is results only.** Logs are structured JSON on stderr.
   Blast: `s3fz query ... | xargs` and fzf parsing break.
8. **Runs from GitHub with `uvx`.** `uvx --from git+https://github.com/ejoliet/s3fz s3fz --help` must exit 0. No compiled build step of our own.
   Blast: the only install path Emmanuel supports stops working.
9. **Shared-cache shards are immutable; only the manifest is mutable, and only via `If-Match` conditional PUT.** Never overwrite a shard, never PUT the manifest unconditionally, never publish rows this session did not list live.
   Blast: two laptops silently erase each other's listings; a colleague's index shows a folder as it was last week with no warning.
10. **Core never imports Pro; Pro imports only `s3fz.api`.** Pro features (`s3fz-pro`) attach through `s3fz.plugins` entry points; a missing licence disables Pro only (exit 5).
    Blast: the MIT core ships paid code, or the paid package breaks free users — both end the open-core model.
11. **Docs are generated from code tables.** Key bindings come from `ui/keymap.py`, query grammar from `query/tokens.py`, env vars from `config.py`. `tests/test_docs_sync.py` fails on drift.
   Blast: users follow docs that lie; tips stop working silently.

> If an instruction below conflicts with an invariant, the invariant wins.

---

## 🔥 Recently Burned

<!-- AIDEV-BURN: date each; prune quarterly. -->

- **2026-10-25** (spec review): v1 schema used `PRIMARY KEY` + upsert → deleted S3 objects never left the index, and ART indexes slowed 10 M-row loads. → Invariant 4; no ART indexes unless a perf test fails.
- **2026-10-25** (spec review): v1 used `jaro_winkler_similarity` for fuzzy → full-table similarity scan per keystroke. → `~abc` compiles to subsequence `ILIKE '%a%b%c%'`.

---

## ✅ Workflow Expectations

- Confirm a short plan before edits touching > 1 module.
- Gate-0 in `README.md` must have a recorded PASS before Phase 0 code.
- Load the matching skill before touching its module (table below).
- Done means: `uv run pytest -q`, `uv run ruff check .`, `uv run mypy --strict src` all pass.
- Do not spend tokens on: manual fzf sessions, real AWS calls, GitHub pushes/PRs, rendering GIFs, installing heavy deps. Print the exact commands for Emmanuel instead, under a `## Run this` heading in your final message.
- Behaviour change in keys, grammar, env vars, or CLI → run `s3fz-tech-writer` skill and update `docs/` in the same change. Flag README/RELEASE notes if affected.

| Module | Skill |
|--------|-------|
| `src/s3fz/crawl/` | `.claude/skills/s3fz-lister` |
| `src/s3fz/index/` | `.claude/skills/s3fz-index` |
| `src/s3fz/query/` | `.claude/skills/s3fz-query` |
| `src/s3fz/ui/`, `actions.py`, completion | `.claude/skills/s3fz-fzf-ui` |
| `src/s3fz/shared/` | `.claude/skills/s3fz-shared-cache` |
| `src/s3fz/api.py`, plugin entry points, `s3fz-pro` repo | `.claude/skills/s3fz-pro-plugins` |
| `docs/`, `--help` text, README usage sections | `.claude/skills/s3fz-tech-writer` |

---

## 🧭 Conventions

- `AIDEV-NOTE:` / `AIDEV-INVARIANT:` / `AIDEV-PERF:` comments are anchors. Preserve them when refactoring. Add `AIDEV-PERF:` wherever a choice exists only for speed.
- Keys are stored without leading `/`; prefixes always end with `/` except root `''`.
- Times are UTC `TIMESTAMPTZ`; display in local time only in the UI layer.
- `ext` is lowercased text after the last `.` of `name`; compound `.fits.gz` → `ext='gz'`, plus `ext2='fits.gz'`.
- Exit codes are a contract: 0 ok · 1 usage · 2 AWS auth/permission · 3 index locked · 4 fzf missing · 5 Pro licence.

---

## 📦 Stack (inferred — see pyproject.toml)

- Lang: Python 3.11+ · Pkg: uv · Test: pytest + moto · Lint: ruff, mypy --strict
- Hot paths: obstore (Rust listing) · DuckDB (index/query) · fzf ≥ 0.66 (UI, Unix-socket `--listen`) · curl (fzf → server)
- Target: macOS (bash, incl. Apple bash 3.2) and Linux. No Windows.

---

## 🚫 Out of Scope for the Agent

- No FUSE mount, no write operations, no hosted/shared index (README Non-Goals).
- Don't put P1–P11 (README v2 Pro table) into the core repo, even behind a flag.
- Don't add Textual/ratatui UI, a search server (Meilisearch, Typesense), or Postgres. fzf + DuckDB until a perf test proves otherwise.
- Don't run against real buckets. Use moto; Emmanuel runs real-bucket checks.
- Don't regenerate `uv.lock` without saying why.
