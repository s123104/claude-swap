# Changelog

All notable user-facing changes to claude-swap are documented here.

Release version is defined in `pyproject.toml` (currently `0.16.0b1+haotool.1`).

## [Unreleased]

### Added

- **Upstream v0.16.0b1 merged** (`--share-history`, upstream #80 plus the
  author's hardening pass): `cswap run --share-history` links `projects/` and
  `history.jsonl` from `~/.claude` into the session profile so every account
  sees one unified conversation history. History a profile already accumulated
  is merged into `~/.claude` first (skipped while a live session holds the
  profile); stale share manifests can only ever unlink symlinks, never real
  history; seeded history files/dirs match Claude Code's own 0600/0700 modes.
  POSIX-only — the flag is rejected on Windows, where copy-mode sharing would
  fork history instead of sharing it. Merged clean: no fork-side code needed
  changes, and upstream's new session code passes the fork's global
  `mypy --strict` untouched.

### Fixed

- **Rotated OAuth tokens survive a wedged lock holder:** when persisting a
  just-refreshed single-use token cannot take the file lock within its 30s
  budget, the rotation is parked in a slot-tagged, owner-only pending file
  next to the backup and applied automatically by the next list/status/switch
  pass over the slot. A re-login or import that lands after the park wins over
  the parked rotation. Previously the token was dropped with only a log
  record, degrading the slot to manual re-login.
- **Windows service hardening (8 fixes):**
  - `service uninstall` stops the running monitor before unregistering the
    task, so uninstall no longer strands an orphan monitor that blocks the
    next install.
  - The monitor's Windows PID probes (`tasklist` / PowerShell CIM) are
    bounded by a 10s timeout and no longer flash console windows under
    `pythonw`; a wedged probe reads as "undeterminable" instead of stalling
    a supervised start forever.
  - The Task Scheduler watchdog re-fire is anchored to a time trigger, so it
    also covers the logon session the service was installed in — the logon
    trigger alone only armed at the next sign-in.
  - `service install` warns that Task Scheduler cannot forward
    `CLAUDE_CONFIG_DIR` into the monitor process.
  - Redirected/piped output on Windows degrades to `errors="replace"`, so
    `cswap --list > file` cannot crash on tree glyphs under a cp1252 locale.
  - The WSL keepalive suggestion is now `sleep infinity`, which ships with
    coreutils on the default Ubuntu image — `dbus-launch` (dbus-x11) does
    not.
  - The shared decision log keeps appending when a size-cap rollover is
    refused by a concurrent holder (Windows sharing violation) instead of
    silencing every record after it.
  - The task XML scopes its logon trigger to the installing user
    (`DOMAIN\user`), system binaries resolve under `%SystemRoot%` rather
    than PATH, and a failed task query reports the failure instead of
    "not installed".
- **macOS purge sweeps both credential backends:** account removal and
  `--purge` delete Keychain items *and* fallback `.enc` files
  unconditionally, instead of trusting the per-process capability cache to
  know which backend past runs wrote to.
- **Monitor survives wedged locks and torn reads:** a `FileLock` held past
  its timeout by a concurrent switch/list, or a transient read error racing
  an `os.replace` writer (Windows sharing violations), maps to the
  usage-unavailable backoff and retries next cycle — the service adapters
  previously treated the escaping exception as fatal.
- **`sync_live_to_backup` keeps its never-raises promise:** a busy lock
  (`LockError`) during the live→backup sync is logged and swallowed like
  every other environmental failure, instead of aborting the surrounding
  list/status/switch pass.
- **launchd reinstall rides out the teardown race:** `service install` over a
  loaded agent retries a `bootstrap` that fails with rc=5 — launchd tears the
  previous instance down asynchronously after `bootout`, and an immediate
  bootstrap can land in that window — up to three times, 0.5s apart, instead
  of leaving the agent installed but not loaded. Any other failure still
  surfaces immediately.
- **Monitor backs off on persistent switch failures:** consecutive failed
  switch attempts raise the retry interval exponentially (capped at 300s)
  instead of re-paying a full plan and forced-refresh churn every poll cycle
  while pinned at the threshold. Success, idle, or a wake gap resets the
  backoff.
- **A consumed rotation survives failed backup verification:** when the
  backup write cannot be verified after a network refresh has already
  consumed the single-use refresh token, the rotation is parked in the
  slot's pending file (the same recovery path as a wedged lock) before the
  error surfaces, instead of being lost with the error.
- **Windows PID probe survives localized `tasklist` output:** the CSV is
  parsed with the `csv` module (quoted image names may contain commas) and
  "no process owns the PID" is decided structurally — no data row carries
  the queried PID — instead of matching the English-only `INFO:` notice.
- **Stale PID files are reclaimed by atomic rename:** the reclaim captures
  the file under a unique temp name, so exactly one reclaimer wins the race
  and a racer's fresh PID file is restored with no-overwrite semantics
  instead of deleted — closing the read-verify-unlink window that could let
  two monitors run at once.
- **Threshold triggers refuse masked stale readings:** when a trusted prior
  cache row masks this cycle's failed usage fetch, the monitor holds instead
  of switching on a pct that may be arbitrarily old, and switches only once
  a fresh fetch succeeds.
- **A lost pending-rotation recovery race is quiet:** when two concurrent
  passes race to recover the same parked rotation, the loser's read of the
  already-consumed file logs at debug instead of warning "Discarding
  unreadable pending credential rotation" over a rotation that was in fact
  applied.
- **An honored Retry-After no longer reads as a wake gap:** the wake-gap
  detector accounts for the interval the monitor was told to sleep, so
  honoring a long server backoff (up to 300s > the 4x poll-ceiling window)
  no longer resets the failure count and velocity baseline on wake. A real
  machine sleep past the planned interval still resets.

### Changed

- **mypy config collapsed to global strict:** the per-module override list is
  gone; the config-driven run (pre-commit) now enforces exactly what CI does.
  Internal only — no behavior change.
- **Docs drift fixed:** README/CHANGELOG version strings track `pyproject.toml`
  again, CONTRIBUTING's mypy notes match the strict config, and the
  upstream-sync doc points at the `converge/*` branch flow.
- **Test hardening:** the `--import` rollback failure matrix and the service
  backends' status/logs/error surfaces are now covered (transfer 82% → 95%;
  launchd/systemd/task_scheduler 98–100% line coverage).
- **Author-style convergence:** fork-only modules carry contract docstrings in
  upstream's voice, spellings follow upstream's American convention, and
  `switcher.py`'s indentation, docstrings, and import prologue were re-aligned
  with upstream — shrinking the shared-file diff against `upstream/main` by
  ~240 lines. Comments/docs only, no behavior change.
- **Auto-switch config seam is typed:** `get_auto_switch_config` /
  `ensure_auto_switch_enabled` / `set_auto_switch_config` return the
  sequence-store `AutoSwitchConfig` dataclass instead of a `dict[str, Any]`,
  so the monitor, CLI, and TUI read `.enabled` / `.threshold` under
  mypy-strict. Internal typing only — no behavior change.
- **Monitor's sequence seam is typed:** the `MonitorHost` protocol and
  `usage_policy.pick_best_from_snapshots` consume the sequence-store
  `SequenceData` view instead of a raw `dict[str, Any]`. The raw-dict shim
  remains for the list/migration/transfer consumers. Internal typing only —
  no behavior change.
- **Test suite reorganized:** the two largest test files were split by
  feature (switch path, broken-slot resilience, add-account, org migration,
  purge, monitor PID lifecycle, poll cadence) and the obvious copy-paste
  tables converted to `pytest.mark.parametrize`. No assertion was removed;
  coverage is unchanged.
- **CI registers a real scheduled task on Windows:** a new
  `windows-task-scheduler` job round-trips the production task XML through
  `Register-ScheduledTask` under a run-unique name (no `Start-ScheduledTask`)
  and asserts it queries back as loaded before unregistering. Marked
  `continue-on-error` until its first green run.

## [0.15.1+haotool.1] — 2026-07-03

### Changed

- **Upstream v0.15.1 merged** (plus upstream `main` as of 2026-07-03):
  - OS-native TLS trust via `truststore` (upstream #78), fixing
    inactive-account token refresh behind corporate/AV-intercepted TLS,
    which the fork's background monitor is especially exposed to.
  - `--switch-to` onto the current account is a no-op; `--force` restores the
    stored credentials over the live login (upstream #79 design, superseding
    the fork's interim same-slot guard). `--import` over the live login now
    hints at `--switch-to <slot> --force`.
  - Upstream Windows test fixes adopted; the Windows CI job is now blocking.
- **`SequenceStore` extracted:** `sequence.json` state is now a typed, immutable
  model (`SequenceData` / `AccountRecord` / `AutoSwitchConfig`) behind a
  lock-agnostic store, replacing raw-dict access throughout the account
  add/remove/switch/auto-switch paths in `switcher.py`. On-disk shape is
  unchanged (unknown/future keys and key presence preserved). Internal refactor
  only — no user-facing behavior change. The new module is mypy-strict; the
  broader `switcher` typing debt is now also resolved (see below).
- **`switcher` is now mypy-strict:** the `ignore_errors` carve-out for
  `claude_swap.switcher` was removed after typing its `sequence.json`,
  usage-cache, and config-dict flows and fixing the `ListHost` /
  `SwitchCliHost` protocol signatures. Internal typing only — no behavior change.
- **Whole package is now mypy-strict:** `switch_cli` and `tui` were typed too
  (a `_RotationParams` TypedDict, precondition None-narrowing, and `curses.window`
  annotations), removing the last `ignore_errors` entry — all 35 modules now pass
  `mypy --strict`. Typing only — no behavior change.
- **Switch paths converged:** the CLI strategy dispatch and the switcher now
  share one switch-decision implementation, and four test-only shims were
  removed from production code. Internal refactor — no behavior change.

### Fixed

- **Cross-account backup poisoning on macOS:** an active-credential read that
  fell back to a leftover plaintext file while the Keychain was locked is
  classified as degraded and is never synced into the active slot's backup —
  the file may hold another account's tokens.
- **`--import --force` onto the active slot** is no longer silently undone by
  the next `--list`/`--status` live→backup sync: the sync skips when the
  backup is at least as new as the live credential (import-wins, #79
  semantics).
- **Rotated single-use tokens under lock contention:** the persist waits out
  legitimate lock holders (30s budget) and re-checks the slot's refresh-token
  lineage under the lock, so contention no longer drops the only working
  credential and a mid-refresh re-login is never clobbered.
- **Monitor PID-file acquisition race:** the stale-file path re-verifies the
  PID file still holds the dead PID before unlinking it, so two monitors can
  no longer both win the singleton; the PID holder is identified by an argv
  fingerprint instead of loose substrings, eliminating recycled-PID false
  positives.
- **Windows Task Scheduler service:** the task XML drops the schema-invalid
  `EnvironmentVariables` block (service mode travels on argv), overrides the
  hostile schema defaults (72h `ExecutionTimeLimit`, battery kill switches),
  and repeats the logon trigger every five minutes with `IgnoreNew` so a dead
  monitor is restarted while a healthy one is never disturbed.
- **WSL keepalive guidance** now suggests `dbus-launch true` — a command that
  actually leaves a resident process holding the WSL instance open.
- **Usage cache after account removal:** removing a slot reclaims its
  `usage.json` row, and cache freshness compares the managed subset instead of
  requiring exact key-set equality — the 15s TTL cache works again after any
  removal, and the auto-switch refresh gate no longer refetches
  already-answered slots every cycle.
- **Keychain-unavailable classification** is preserved across the list/status
  facades, so a locked Keychain shows as unavailable instead of
  "no credentials".
- **Credential write verification** checks the store the write actually landed
  on, and `add_account` re-resolves the slot under the lock; the OAuth
  User-Agent is bound to the package version.

## [0.15.0b2+haotool.1] — 2026-06-28

### Added

- **Auto-switch at usage limit (Beta):** TUI menu, `cswap --monitor`, and macOS launchd background service. Fail-closed target selection from trusted usage snapshots.
- **`cswap --health`:** account health, usage, and OAuth token status.
- **TUI Watch:** live status and usage dashboard (in-place rendering aligned with upstream).
- **Upstream sync:** `--json`, `--strategy`, `assume_yes`, managed API-key accounts.

### Changed

- **Credential layer:** `CredentialStore`, `CredentialRefresher`, and usage-cache codec extracted from switcher; aligned with upstream Keychain routing.
- **Switch paths:** cooldown-aware auto-switch planning, SwitchIntent types, OAuth verify-on-activation.

### Fixed

- OAuth inactive-account refresh serialized under FileLock.
- Per-window usage tracking so monitor velocity is not masked by saturated holds.
- macOS Keychain account name and `.enc`-wins backup reconciliation aligned with upstream.

### Upgrade notes

Auto-switch users: see [0.13.1] breaking changes and README failure-modes section before enabling monitor/service.

## [0.13.1] — 2026-06-14

Historical note: auto-switch, `--health`, and related fork features ship in **[0.15.0b2+haotool.1]** above.

### Fixed

- **Session mode on Windows:** session validation now resolves `claude` via `shutil.which` so `.cmd` shims are found (upstream PR #54).

### Breaking — `switch()` API (auto-switch beta)

Automated switching now uses explicit **SwitchIntent** types instead of boolean kwargs.

| Before | After |
|--------|-------|
| `switch(quiet=True)` | `switch(BackgroundAutoSwitchIntent(decision=...))` |
| `switch(prefer_least_busy=True)` | `switch(InteractiveAutoSwitchIntent(decision=...))` or `BackgroundAutoSwitchIntent(...)` with a decision from `build_auto_switch_decision()` |
| `switch()` returned `None` | `switch()` returns `bool` — `True` when credentials changed, `False` when no switch was needed |

**Automated switching is fail-closed.** When usage snapshots are cold or expired, the monitor will not round-robin blindly; it logs `no trusted usage snapshots` and holds until cache is warm. Manual `cswap --switch` still uses round-robin.

### Upgrade steps (auto-switch users)

1. **Before upgrading:** run `cswap --list` on every machine with auto-switch enabled (seeds per-slot `_cached_at` snapshots).
2. **After upgrading:** run `cswap service install` (macOS background users), then `cswap service status`.
3. **External callers** of `ClaudeAccountSwitcher.switch()`: pass a `SwitchIntent` and handle the `bool` return value. Import intents from `claude_swap.models`.

See also the [Failure modes and upgrade](README.md#failure-modes-and-upgrade) section in README.
