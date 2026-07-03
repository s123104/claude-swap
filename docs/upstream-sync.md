# Upstream sync

Short reference for keeping `publish/clean` in sync with
[realiti4/claude-swap](https://github.com/realiti4/claude-swap) `main`.

Since 2026-07-03 (`fa7eddf`) the branch is **merge-tracked, not regenerated**:
upstream commits are merged in with their original authorship, fork work lands
on top as conventional commits, and history is never rewritten. The old
"replay a clean linear stack + force-with-lease push" flow is retired — it
required force pushes and flattened upstream contributors' commits.

## Flow

Integration happens on a `converge/*` branch; `publish/clean` only ever
fast-forwards to it.

```bash
git fetch upstream

# 1. Merge upstream into the integration branch.
git checkout converge/<current>
git merge upstream/main
#    Typical conflicts are version-only: keep upstream's version plus the
#    fork's PEP 440 local label (e.g. 0.16.0b1+haotool.1) in pyproject.toml,
#    then regenerate the lockfile — never hand-edit it:
uv lock

# 2. Gate the merged tree (same bar as CI).
uv run ruff check src tests
uv run mypy --strict src/claude_swap
uv run pytest -q --cov=claude_swap

# 3. Record the sync in CHANGELOG.md and align the version strings
#    (README version table, CHANGELOG header) with pyproject.toml.

# 4. Fast-forward the publish branch and push (no force needed, ever).
git checkout publish/clean
git merge --ff-only converge/<current>
git push haotool publish/clean
```

## Review

```bash
git log upstream/main..publish/clean --oneline   # fork-only commits
git merge-tree --write-tree HEAD upstream/main   # dry-run future merges
```

## Safety

- `publish/clean` must always be a fast-forward of the integration branch;
  if it is not, the integration branch is stale — rebase nothing, re-merge.
- A merge is only pushed after ruff + `mypy --strict` + the full test suite
  pass on the merged tree.
- Force pushes to `publish/clean` are no longer part of the flow. The old
  regeneration-era backups (`backup/pre-*`) remain as historical snapshots.
