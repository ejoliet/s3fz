#!/usr/bin/env bash
# Gate-0 manual proxy (README "Gate-0"). No project code: s5cmd + duckdb + fzf only.
# Usage: scripts/gate0.sh s3://bucket/prefix/           (public bucket: S5CMD_FLAGS=--no-sign-request)
# Paste the printed timings into README.md > Gate-0, then mark PASS or FAIL.
set -euo pipefail
uri="${1:?usage: gate0.sh s3://bucket/prefix/}"
uri="${uri%/}/"                                   # exactly one trailing slash
prefix="${uri#s3://*/}"                           # key prefix without bucket ('' for bucket root)
work="${GATE0_DIR:-/tmp/s3fz-gate0}"; mkdir -p "$work"; cd "$work"
for t in s5cmd duckdb fzf; do command -v "$t" >/dev/null || { echo "missing $t (brew install $t)"; exit 1; }; done

echo "== 1. full listing of $uri (PASS if < 10 min)"
# s5cmd >= 2.x: recursive listing is the wildcard form, there is no -r. Output: date time size key-relative-to-prefix
/usr/bin/time -p s5cmd ${S5CMD_FLAGS:-} ls "${uri}*" > keys.txt
wc -l keys.txt

echo "== 2. load into DuckDB"
duckdb bucket.duckdb <<SQL
CREATE OR REPLACE TABLE o AS
SELECT strptime(regexp_extract(line, '^(\S+ \S+)', 1), '%Y/%m/%d %H:%M:%S') AS mtime,
       regexp_extract(line, '^\S+ \S+\s+(\d+)', 1)::BIGINT AS size,
       '$prefix' || regexp_extract(line, '^\S+ \S+\s+\d+\s+(.*)$', 1) AS key
FROM read_csv('keys.txt', columns={'line':'VARCHAR'}, delim='\x01', header=false, quote='')
WHERE line NOT LIKE '%  DIR  %';
ALTER TABLE o ADD COLUMN ext VARCHAR;
UPDATE o SET ext = lower(regexp_extract(key, '[^.]\\.([^.]+)$', 1));
SELECT count(*) AS keys, min(mtime), max(mtime) FROM o;
SQL

echo "== 3. queries (PASS if each < 500 ms)"
duckdb bucket.duckdb <<'SQL'
.timer on
SELECT key FROM o ORDER BY mtime DESC LIMIT 50;
SELECT count(*) FROM o WHERE key LIKE '%\_cal.asdf' ESCAPE '\';
SELECT count(*) FROM o WHERE ext = 'fits' AND key LIKE 'roman/%';
SQL

echo "== 4/5. interactive (PASS if keystrokes feel < 200 ms). Esc to quit."
fzf --disabled --prompt 'deep> ' \
  --bind 'change:reload(duckdb bucket.duckdb -csv -noheader -c "SELECT key FROM o WHERE key ILIKE ''%{q}%'' ORDER BY mtime DESC LIMIT 200")' \
  --bind 'start:reload(duckdb bucket.duckdb -csv -noheader -c "SELECT key FROM o ORDER BY mtime DESC LIMIT 200")' || true
echo "Record: listing time, key count, query timings, and PASS/FAIL in README.md > Gate-0."
