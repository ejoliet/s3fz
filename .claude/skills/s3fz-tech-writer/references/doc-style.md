# Doc templates

## Cookbook recipe

```markdown
### Find every file for one exposure

Exposure IDs are filename prefixes, so search by prefix:

    s3fz s3://demo-mission/roman/l2/

Press `ctrl-f` (deep search), type `r001101*`.

Narrow to one filter and calibrated products: `r001101* f146 ext:asdf`.

Why it's fast: a literal prefix compiles to `starts_with`, which never compares the rest of the name.
```

## Tip

```markdown
> 💡 Tip — type `~f146` to match f…1…4…6 in order anywhere in the name, like fzf's fuzzy mode, but across the whole bucket.
```

## Troubleshooting entry

```markdown
### Search finds nothing in a folder I know is full

**Cause:** that folder isn't indexed yet (shown with `…` in the list).
**Fix:** open it once, or press `ctrl-r` on its parent. Check coverage with `alt-i`.
```

## index.md skeleton

1. One-line description.
2. Install: `uvx --from git+https://github.com/ejoliet/s3fz s3fz s3://bucket/` and `uv tool install ...`.
3. Requirements: fzf ≥ 0.66, curl, AWS credentials.
4. First search in 60 seconds (3 steps, one GIF).
5. Where next: cookbook, query language, keys, tips.
