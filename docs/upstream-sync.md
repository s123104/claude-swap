# Upstream sync

Short reference for regenerating `publish/clean` from [realiti4/claude-swap](https://github.com/realiti4/claude-swap) `main`.

## When to run

After merging upstream changes or when preparing a clean publish branch from integration work (e.g. `improve/p1-clean-code-convergence`).

## Steps

```bash
git fetch upstream
./scripts/build-clean-history.sh [SOURCE] [TARGET]
# defaults: SOURCE=improve/p1-clean-code-convergence  TARGET=publish/clean
```

Review the log:

```bash
git log upstream/main..publish/clean --oneline
uv run pytest
```

Push:

```bash
git push --force-with-lease haotool publish/clean
```

## Safety

- The script creates `backup/pre-clean-rewrite-YYYYMMDD` from `SOURCE` before rewriting.
- Use `--force-with-lease`, not bare `--force`.
- Do not delete the backup until the remote branch looks correct.
