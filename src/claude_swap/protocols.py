"""Structural host Protocols for narrow switcher dependencies.

Consumers depend on these views instead of the full ``ClaudeAccountSwitcher`` so
tests can inject minimal fakes and production code stays decoupled from switcher
internals it does not call. Underscore-prefixed members are part of the host
contract on purpose: the leading underscore marks "not a stable public CLI
surface", not "private to one module" — these views are the seam.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, Protocol

from claude_swap.models import (
    SwitchIntent,
    SwitchPreconditions,
)
from claude_swap.credentials import ActiveCredentials

ServiceState = Literal["not installed", "installed but not loaded", "loaded"]


class RefreshHost(Protocol):
    """Switch surface ``CredentialRefresher`` uses for OAuth read/write paths."""

    lock_file: Path
    credentials_dir: Path
    _logger: logging.Logger

    def _read_credentials(self) -> str | None: ...
    def _read_account_credentials(self, account_num: str, email: str) -> str: ...
    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str,
    ) -> None: ...
    def _live_session_pids(self, account_num: str, email: str) -> list[int]: ...


class ServiceHost(Protocol):
    """Minimal switcher view the service layer needs: just the backup root.

    The supervisor backends only resolve log/artifact paths under ``backup_dir``,
    so they depend on this one-field view instead of the whole switcher.
    """

    backup_dir: Path


class ServiceBackend(Protocol):
    """Platform-native supervisor for the auto-switch engine.

    Methods take a ``ServiceHost`` (not the concrete switcher); the underscore is
    not relevant here — this is the public supervisor contract.
    """

    def install(self, switcher: ServiceHost) -> int: ...

    def uninstall(self, switcher: ServiceHost) -> int: ...

    def state(self) -> ServiceState: ...

    def status(self, switcher: ServiceHost) -> int: ...

    def logs(self, switcher: ServiceHost, lines: int = 40) -> int: ...


class ListHost(Protocol):
    """Switch surface ``list_reporter`` uses for read-only list/status rendering."""

    sequence_file: Path
    lock_file: Path
    credentials_dir: Path
    _logger: logging.Logger

    @property
    def usage_cache_path(self) -> Path: ...

    def _get_sequence_data_migrated(self) -> dict[str, Any] | None: ...
    def _get_current_account(self) -> tuple[str, str] | None: ...
    def _find_account_slot(
        self, data: dict[str, Any], email: str, organization_uuid: str,
    ) -> str | None: ...
    def _read_credentials(self) -> str | None: ...
    def _read_active_credentials(self) -> ActiveCredentials: ...
    def _read_account_credentials(self, account_num: str, email: str) -> str: ...
    def _write_credentials(self, credentials: str) -> None: ...
    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str,
    ) -> None: ...
    def _sync_live_account_credentials_to_backup(
        self, account_num: str, email: str, credentials: str,
    ) -> None: ...
    def _refresh_inactive_credentials_if_needed(
        self, account_num: str, email: str, credentials: str,
    ) -> tuple[str, str | None]: ...
    def _live_session_pids(self, account_num: str, email: str) -> list[int]: ...
    def _get_display_tag(
        self, email: str, org_name: str, org_uuid: str,
    ) -> str: ...
    def _first_run_setup(self) -> None: ...
    def _usage_cache_fresh(
        self, cached: dict[str, Any], account_keys: set[str],
    ) -> bool: ...
    def _merge_usage_cache(self, updates: dict[str, object]) -> None: ...


class SwitchCliHost(Protocol):
    """Switch surface ``switch_cli`` uses for strategy-aware CLI dispatch."""

    def _classify_switch_preconditions(self) -> SwitchPreconditions: ...
    def _find_account_slot(
        self, data: dict[str, Any], email: str, organization_uuid: str,
    ) -> str | None: ...
    def _resolve_fresh_machine_target(
        self,
        *,
        quiet: bool = False,
        warnings: list[str] | None = None,
    ) -> str: ...
    def _perform_switch(
        self,
        target_account: str,
        *,
        intent: SwitchIntent | None = None,
        emit_output: bool = True,
    ) -> dict[str, Any] | None: ...
    def _switch_result_from_op(
        self, op: dict[str, Any], strategy: str, extra_warnings: list[str] | None = None,
    ) -> dict[str, Any]: ...
    def _switch_noop(
        self,
        *,
        strategy: str,
        reason: str,
        message: str,
        from_ref: dict[str, Any] | None = None,
        to_ref: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> dict[str, Any]: ...
    def _switch_unmanaged_notice(self, current_email: str) -> None: ...
    def _select_best_switchable(
        self, current_num: str | None,
    ) -> tuple[str | None, str]: ...
    def _switch_manual_rotation_target(
        self,
        sequence: list[Any],
        anchor: str | int | None,
        *,
        quiet: bool,
        skip_exhausted: bool = False,
        warnings: list[str] | None = None,
    ) -> tuple[str | None, bool]: ...
