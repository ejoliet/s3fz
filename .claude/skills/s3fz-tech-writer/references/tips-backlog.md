# Tips backlog (verify against code before publishing)

Ordered by payoff.

1. **Two search modes.** Directory mode filters what you see; `ctrl-f` deep search queries everything indexed below the current folder.
2. **Globs match filenames.** `r001101*` = starts with, `*_cal.asdf` = ends with, `*f146*` = contains. Add a `/` to match the full path: `roman/*/r0011*`.
3. **Plain text is exact substring, not fuzzy.** `f146` won't match `f_1_4_6`. Use `~f146` when you want fuzzy.
4. **Smart case.** Lowercase matches any case; one uppercase letter makes the token case-sensitive.
5. **Stack filters.** Tokens AND together: `r001101* f146 ext:asdf mtime>1d`. `|` ORs two tokens: `f146 | f158`. `!` negates: `!ext:txt`.
6. **Newest first by default.** `ctrl-s` cycles name and size. Folders sort by their newest file anywhere inside.
7. **Pipe results.** `s3fz query s3://b/ 'r001101* ext:asdf' --tsv | cut -f1 | s5cmd run` style workflows; `--json | jq` for scripts.
8. **Multi-select then act.** `tab` marks rows; `ctrl-y` copies all URIs, `ctrl-d` downloads all.
9. **Seed from Inventory.** If the bucket has S3 Inventory, `--inventory` loads millions of keys in seconds; s3fz then refreshes only what you open.
10. **Coverage matters.** `…` means not indexed yet. `alt-i` shows coverage; `s3fz index s3://b/prefix/ --wait` warms a subtree before a session.
11. **Fresh view.** `ctrl-r` re-lists the current folder now; otherwise s3fz re-lists after `S3FZ_TTL` (1 h).
12. **Tab completion.** `eval "$(s3fz completion bash)"` in `~/.bash_profile`, then `s3fz s3://bucket/ro<Tab>`.
13. **Literal % or _ in names** need no escaping; s3fz escapes them for you.
14. **Other S3 endpoints.** `S3FZ_ENDPOINT_URL` for MinIO or other S3-compatible stores.
