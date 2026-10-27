---
name: s3fz-fzf-ui
description: How to build and change the s3fz terminal UI (src/s3fz/ui/, actions.py, shell completion) — fzf ≥ 0.66 process, --listen Unix socket, the curl-driven query server, directory vs deep-search modes, key bindings, preview, download/copy/presign actions, bash completion. Use whenever touching fzf flags, keys, preview, the HTTP-over-socket server, live refresh, or tab completion.
---

# s3fz fzf UI

fzf draws, filters, and handles keys. Python serves data over a Unix socket and tells fzf what to do next. No Python or DuckDB process starts per keystroke (Invariant 2).

## Processes and sockets

| Piece | Socket | Who talks |
|-------|--------|-----------|
| s3fz query server (`ui/server.py`) | `$S3FZ_CACHE_DIR/<bucket>.<pid>.sock` exported as `S3FZ_SOCK` | fzf bindings via `curl --unix-socket` |
| fzf | `--listen <tmpdir>/fzf.<pid>.sock` (path must end in `.sock`; fzf deletes any existing file at that path) | s3fz POSTs actions (live refresh, header updates) |

Server: stdlib `socketserver.ThreadingUnixStreamServer` + `http.server.BaseHTTPRequestHandler`. No web framework. Endpoints return either rows (TSV) or an fzf action string (for `transform`). Socket files are `0600` and removed on exit (try/finally + signal handlers).

## Row format

`<mtime local "YYYY-MM-DD HH:MM">\t<size human, right-aligned 7>\t<display path relative to scope; prefixes end in "/">\t<full key>`
fzf flags: `--delimiter '\t' --with-nth 1..3 --accept-nth 4 --nth 3 --exact --ansi`. Prefix rows are dimmed and show `max_mtime` and object count instead of size. `🔒` marks denied prefixes; `…` marks cold ones.

## Modes

The server owns mode state. Keys call `transform(curl ...)`; the server returns the action chain.

| Mode | fzf state | Data source |
|------|-----------|-------------|
| directory | search enabled, `--exact`, `unbind(change)` | `reload(curl .../dir)` children of current prefix, sorted per `sort` |
| deep | `disable-search`, `rebind(change)` | `change:reload-sync(curl .../query --data-urlencode q={q})` over the whole scope |

Quoting: fzf substitutes `{q}` already shell-quoted. Write `--data-urlencode q={q}`, never `"q={q}"` (the literal quotes end up in the query).

## Key map (source of truth: `ui/keymap.py`, drives docs)

| Key | Action |
|-----|--------|
| `enter` | prefix → enter it (`reload`, `clear-query`, `change-prompt`); object → accept, print `s3://` URI(s) |
| `backward-eof` (Backspace on empty query) | parent prefix |
| `ctrl-f` | toggle directory ↔ deep search |
| `tab` / `shift-tab` | multi-select (fzf default; do not rebind) |
| `ctrl-s` | cycle sort: newest → name → largest |
| `ctrl-r` | re-list current prefix now |
| `ctrl-y` | copy URI(s) (`pbcopy`, `wl-copy`, `xclip -sel clip`, first found) |
| `ctrl-d` | download selected to `$PWD` (never overwrite: add `.1`, `.2`) |
| `alt-p` | print 15-min presigned URL(s) and exit |
| `ctrl-/` | toggle preview |
| `alt-i` | index coverage for current subtree in preview |
| `ctrl-h` | team history / saved searches via `/history` endpoint (when `S3FZ_SHARED` set) |
| `esc` | quit |

Avoid rebinding fzf defaults people rely on: `ctrl-p`/`ctrl-n` (move), `ctrl-j`/`ctrl-k`, `tab`.

## Live refresh

When the crawler finishes a prefix inside the current view, POST to fzf's socket at most once per second: `reload-sync(...)` keeps cursor flicker down. Update the header with coverage: `change-header(warm 12/40 · 1.2M objects · 3 cold)`. Never refresh while the user typed in the last 300 ms in deep mode (the `change` binding already reloads).

## Preview

`--preview 'curl ... /preview --data-urlencode key={4}'`. Object: size, mtime, etag, storage class, content-type and user metadata from `HeadObject`, tags; first 2 KB if `ext` in a text whitelist; for `fits` and `asdf`, first header block via a ranged GET (2,880 bytes for FITS). Prefix: counts, `max_mtime`, newest 10 children. Cache HEAD results in memory per session.

## Shell completion

`s3fz widget bash` prints the command-line widget. `READLINE_LINE`/`READLINE_POINT` only exist in bash >= 4, so `bind -x` works there; on Apple bash 3.2 emit the readline-macro variant that fzf's `key-bindings.bash` uses (run the picker inside a command-substitution macro and splice with `\C-y`). Detect with `BASH_VERSINFO[0]` at script-print time.

`s3fz completion bash` prints a script: `complete -o nospace -o default -F _s3fz s3fz`. The function calls `s3fz _complete "$cur"` which answers from the index (read-only, or via `S3FZ_SOCK` if a session is running). Must work on Apple bash 3.2: no `mapfile`, no `compopt`, no associative arrays. Complete only up to the next `/`.

## Tests

- Server endpoints unit-tested with an in-memory index: `/dir`, `/query`, `/toggle`, `/enter`, `/preview` (action strings asserted exactly).
- Bind-string builder tested as plain strings; `fzf --version` ≥ 0.66 check → exit 4 with `brew install fzf` hint.
- Completion script passes `bash -n` and `shellcheck -s bash`.

Real fzf sessions are not run by the agent. Print this for Emmanuel:

```bash
uv run moto_server -p 5555 &
uv run python scripts/demo_bucket.py --endpoint http://localhost:5555   # creates s3://demo-mission/ with realistic names
S3FZ_ENDPOINT_URL=http://localhost:5555 uv run s3fz s3://demo-mission/
```
