---
name: s3fz-query
description: How to build and change the s3fz query language and SQL compiler (src/s3fz/query/) — tokens like r001101*, *f146*, ~f146, ext:fits, mtime>7d, size>1g, !neg, a|b, sort:, globs, smart case, LIKE escaping, scope. Use whenever touching parser.py, compiler.py, tokens.py, search behaviour, "search doesn't find X", or adding a filter.
---

# s3fz query

Turns what the user types in deep-search mode into one parameterised DuckDB statement. The compiler is the only code that builds SQL from user input (Invariant 5).

## Source of truth

`query/tokens.py` holds a list of `TokenSpec(name, pattern, example, sql_doc, help)`. The parser, `--help`, and `docs/query-language.md` are all driven from it. Adding a token = one `TokenSpec` + compiler branch + tests + regenerated docs.

## Grammar

Whitespace-separated tokens, ANDed. `|` between two tokens ORs them (fzf convention). Leading `!` negates any token. Quoting with `"..."` keeps spaces.

| Token | Matches | Compiles to |
|-------|---------|-------------|
| `f146` | substring of `name` | `name ILIKE '%f146%'` |
| `~f146` | fzf-style subsequence of `name` | `name ILIKE '%f%1%4%6%'` |
| `r001101*` | glob on `name`, literal head | `starts_with(lower(name), 'r001101')` |
| `*_cal.asdf` | glob on `name`, literal tail | `starts_with(rkey, reverse('_cal.asdf'))` (case-insensitive variant uses `lower(rkey)`) |
| `*f146*`, `r00*f146*.fits` | general glob on `name` | `name ILIKE 'r00%f146%.fits' ESCAPE '\'` |
| `roman/l2/*/r0011*` | glob containing `/` → on `key` | `key LIKE ... ESCAPE '\'`, plus `starts_with(key, 'roman/l2/')` for the literal head |
| `^roman/l2` / `cal.asdf$` | key prefix / key suffix | `starts_with(key, ?)` / `starts_with(rkey, reverse(?))` |
| `ext:fits,asdf` | extension set; also matches `ext2` (`ext:fits.gz`) | `(ext IN (...) OR ext2 IN (...))` |
| `mtime>7d`, `mtime<2026-10-01`, `mtime:2026-10-20` | relative (`s m h d w`) / absolute / day | `last_modified > now() - INTERVAL '7 days'` etc. |
| `size>1g`, `size<10k` | binary units `k m g t` | `size > 1073741824` |
| `class:GLACIER` | storage class | `storage_class = ?` |
| `sort:mtime` `sort:name` `sort:size` `sort:-size` | order; default `mtime` newest first | `ORDER BY ...` |
| `limit:2000` | row cap, max 10,000 | `LIMIT ?` |

## Rules that are easy to get wrong

1. **Escape before translating.** User text → escape `\`, `%`, `_` with `\` → then `*`→`%`, `?`→`_`. Always emit `ESCAPE '\'`. Mission filenames are full of `_`; without escaping `r_001` also matches `rX001`.
2. **Smart case**, same as fzf: a token with any uppercase letter is case-sensitive (`LIKE`, no `lower`); otherwise case-insensitive.
3. **Prefer `starts_with` for literal heads/tails.** It is cheaper than `LIKE` and needs no escaping. Pass the literal as a parameter.
4. **Scope is implicit.** Every query gets `bucket = ? AND key >= ? AND key < ? || chr(255)` for the current prefix (range form prunes zone maps; `starts_with` may not), unless the token set contains a `/`-glob or `^` anchored at root.
5. **Empty query** in deep mode → newest 500 objects in scope.
6. **Unknown `field:`** → parse error with the closest valid field name. Never silently treat it as a substring.
7. **Everything is a bound parameter.** Only fixed SQL fragments come from code. Column names in `sort:` map through a whitelist dict.
8. **Output columns** fixed: `key, size, last_modified, dir, name` — the UI formats them (`s3fz-fzf-ui`).

## Interfaces

```python
parse(text: str) -> Query                          # raises QueryError(msg, position)
compile_query(q: Query, *, bucket: str, scope: str, limit: int = 500) -> tuple[str, list[object]]
```

`QueryError` is shown in the fzf header, not as a traceback. The previous results stay on screen.

## Tests

Table-driven `pytest.mark.parametrize` over `(text, expected_sql_fragment, expected_params)`, plus execution tests against a small in-memory DuckDB with names like:
`r0011010001001_f146_cal.asdf`, `r0011020001001_f158_cal.asdf`, `R0011010001001_F146_CAL.ASDF`, `r001101_x_y.fits.gz`, `r0011010%weird.fits`, `notes_f146.txt`.

Must-pass cases: `r001101*`, `*f146*`, `~f146`, `r001101* f146 ext:asdf`, `*_cal.asdf`, `!ext:txt`, `f146 | f158`, `F146` (case-sensitive), `r0011010%weird*` (literal `%`), `roman/*/r0011*`, `mtime>2h sort:-size`, `ext:fits.gz`.

Perf: run the must-pass set over 1 M synthetic rows; each < 200 ms (marked `perf`).
