"""Switch-path behavior: post-switch display, rollback, and same-slot/force semantics."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from typing import TYPE_CHECKING

from claude_swap.exceptions import CredentialReadError
from claude_swap.models import Platform

if TYPE_CHECKING:
    from claude_swap.models import BackgroundAutoSwitchIntent
from claude_swap.switcher import ClaudeAccountSwitcher


class TestPerformSwitchPostDisplay:
    """Regression tests for the post-switch display running outside the lock."""

    @staticmethod
    def _background_intent() -> "BackgroundAutoSwitchIntent":
        from claude_swap.models import (
            AutoSwitchDecisionContext,
            BackgroundAutoSwitchIntent,
        )

        return BackgroundAutoSwitchIntent(
            decision=AutoSwitchDecisionContext(
                threshold=95,
                active_usage_pct=None,
                live_active_slot="1",
                sequence_active_slot="1",
                usage_by_slot={},
            ),
        )

    def _setup_two_accounts(
        self,
        temp_home: Path,
        sample_sequence_data: dict,
    ) -> tuple[ClaudeAccountSwitcher, dict, dict]:
        """Set up a switcher with two managed accounts using in-memory
        credential and config stores.

        This bypasses the real macOS Keychain / Windows Credential Manager
        completely so tests never prompt the user for "restore to defaults"
        on macOS and never leak credentials into the developer's keyring.

        Returns (switcher, creds_store, configs_store). Live credentials for
        the active account are written to the temp-home credentials file
        (safe — that file lives in the test's tmp_path).
        """
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        # Live credentials for active account 1 (file under temp_home).
        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })
        (temp_home / ".claude" / ".credentials.json").write_text(live_creds)

        # Expired backup credentials for account 2 — forces refresh in
        # list_accounts() proactive path.
        expired_2 = json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-stale-2",
                "refreshToken": "rt-orig-2",
                "expiresAt": 0,
                "scopes": ["user:profile"],
            },
        })

        # In-memory stores keyed by (num, email).
        creds_store: dict[tuple[str, str], str] = {
            ("2", "account2@example.com"): expired_2,
        }
        configs_store: dict[tuple[str, str], str] = {
            ("2", "account2@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "account2@example.com",
                    "accountUuid": "uuid-2",
                },
            }),
        }
        return switcher, creds_store, configs_store

    @staticmethod
    def _install_store_patches(
        switcher: ClaudeAccountSwitcher,
        creds_store: dict[tuple[str, str], str],
        configs_store: dict[tuple[str, str], str],
        live_state: dict,
    ) -> list:
        """Patch credential/config read/write to use in-memory stores.

        Critically, this also stubs _read_credentials/_write_credentials so
        nothing touches the real macOS Keychain (which would prompt the user
        with "Claude wants to use the confidential information stored in your
        keychain" during the test run).
        """
        def read_creds(num, email):
            return creds_store.get((str(num), email), "")

        def write_creds(num, email, creds):
            creds_store[(str(num), email)] = creds

        def read_cfg(num, email):
            return configs_store.get((str(num), email), "")

        def write_cfg(num, email, cfg):
            configs_store[(str(num), email)] = cfg

        def read_live():
            return live_state.get("creds", "")

        def write_live(creds, *, verify: bool = False) -> None:
            live_state["creds"] = creds
            # Honour the production contract: verify=True must validate the
            # readback. Since both read/write target the same in-memory dict
            # in these tests, the check is trivially satisfied — but the stub
            # must still accept the kwarg or _perform_switch crashes.
            if verify and read_live() != creds:
                # Match the real CredentialWriteError message shape.
                from claude_swap.exceptions import CredentialWriteError

                raise CredentialWriteError(
                    "Credential write verification failed (test stub)"
                )

        patches = [
            patch.object(switcher, "_read_account_credentials", side_effect=read_creds),
            patch.object(
                switcher, "_write_account_credentials", side_effect=write_creds
            ),
            patch.object(switcher, "_read_account_config", side_effect=read_cfg),
            patch.object(switcher, "_write_account_config", side_effect=write_cfg),
            patch.object(switcher, "_read_credentials", side_effect=read_live),
            patch.object(switcher, "_write_credentials", side_effect=write_live),
        ]
        for p in patches:
            p.start()
        return patches

    def test_switch_persists_rotated_refresh_token_to_backup(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """Regression: _perform_switch must persist refreshed credentials to backup.

        Prior to the fix, _perform_switch held the outer FileLock around
        list_accounts(). Inside list_accounts(), the persist closure tried to
        re-acquire the same file lock (different FD, so fcntl.flock is NOT
        re-entrant), spun to the 10s timeout, raised LockError, and the
        refreshed credentials were silently dropped at debug level. If
        Anthropic rotated the refresh token on that request, the backup
        retained the old (now-invalid) refresh token and the only recovery
        was a re-login.

        This test exercises the full _perform_switch path with account 2
        needing a refresh, and verifies the rotated refresh token actually
        landed on disk. Against main this fails; against the fix it passes.
        """
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home,
            sample_sequence_data,
        )
        # The currently-active account 1's creds carry an expired expiresAt.
        # After the swap, account 1 becomes *inactive* and its just-backed-up
        # credentials are eligible for proactive refresh inside the
        # post-switch list_accounts() call. This is the scenario that
        # triggers the original deadlock bug.
        live_state = {
            "creds": json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-live-1",
                        "refreshToken": "rt-orig-1",
                        "expiresAt": 0,
                        "scopes": ["user:profile"],
                    },
                }
            )
        }
        patches = self._install_store_patches(
            switcher,
            creds_store,
            configs_store,
            live_state,
        )

        # Monkeypatch refresh_oauth_credentials to simulate a server-side
        # refresh-token rotation (rt-orig-1 -> rt-rotated-1).
        rotated_creds = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-rotated-1",
                    "refreshToken": "rt-rotated-1",
                    "expiresAt": 9_999_999_999_000,
                    "scopes": ["user:profile"],
                },
            }
        )

        try:
            with (
                patch(
                    "claude_swap.oauth.refresh_oauth_credentials",
                    return_value=rotated_creds,
                ),
                patch(
                    "claude_swap.oauth.request_usage_data",
                    return_value={
                        "five_hour": {"utilization": 12.0, "resets_at": None},
                        "seven_day": {"utilization": 34.0, "resets_at": None},
                    },
                ),
            ):
                switcher._perform_switch("2")
        finally:
            for p in patches:
                p.stop()

        # After switch, backup for account 1 (now inactive) must contain the
        # rotated refresh token — confirming the persist inside list_accounts()
        # actually fired and didn't hit the lock deadlock.
        backup_after = creds_store.get(("1", "test@example.com"), "")
        assert backup_after, "backup credentials for account 1 are missing"
        backup_oauth = json.loads(backup_after)["claudeAiOauth"]
        assert backup_oauth["refreshToken"] == "rt-rotated-1", (
            f"Expected rotated refresh token on disk, got "
            f"{backup_oauth.get('refreshToken')!r} — lock deadlock regression"
        )
        assert backup_oauth["accessToken"] == "sk-rotated-1"

    def test_quiet_switch_suppresses_banners_and_followup(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """BackgroundAutoSwitchIntent suppresses banners and followup:
        launchd's stdout/stderr should not collect interactive banner text or
        the platform-specific 'next message / 30s' followup line.
        """
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home,
            sample_sequence_data,
        )
        live_state = {
            "creds": json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-live-1",
                        "refreshToken": "rt-live-1",
                    },
                }
            )
        }
        patches = self._install_store_patches(
            switcher,
            creds_store,
            configs_store,
            live_state,
        )

        try:
            with (
                patch(
                    "claude_swap.oauth.refresh_oauth_credentials",
                    return_value=creds_store[("2", "account2@example.com")],
                ),
                patch.object(switcher, "list_accounts") as mock_list,
            ):
                switcher._perform_switch("2", intent=self._background_intent())
        finally:
            for p in patches:
                p.stop()

        # Commit happened — sequence advanced.
        data = switcher._get_sequence_data()
        assert data is not None
        assert data["activeAccountNumber"] == 2

        # Output stays empty: no "Switched to", no followup, no list_accounts().
        output = capsys.readouterr().out
        assert "Switched to" not in output
        assert "New account active" not in output
        assert "restart Claude Code" not in output
        mock_list.assert_not_called()

    def test_force_refresh_threads_through_perform_switch(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """BackgroundAutoSwitchIntent must forward force_refresh to
        _refresh_target_credentials_before_activation as force=True.
        Otherwise the monitor's "fresh token after handoff" guarantee is broken.
        """
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home,
            sample_sequence_data,
        )
        live_state = {
            "creds": json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-live-1",
                        "refreshToken": "rt-live-1",
                    },
                }
            )
        }
        patches = self._install_store_patches(
            switcher,
            creds_store,
            configs_store,
            live_state,
        )

        try:
            with (
                patch.object(
                    switcher,
                    "_refresh_target_credentials_before_activation",
                    wraps=switcher._refresh_target_credentials_before_activation,
                ) as spy,
                patch(
                    "claude_swap.oauth.refresh_oauth_credentials",
                    return_value=creds_store[("2", "account2@example.com")],
                ),
                patch.object(switcher, "list_accounts"),
            ):
                switcher._perform_switch("2", intent=self._background_intent())
        finally:
            for p in patches:
                p.stop()

        # The spy should have seen force=True.
        spy.assert_called_once()
        assert spy.call_args.kwargs.get("force") is True

    def test_cli_json_switch_honors_intent_flags(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """``switch(json_output=True)`` routes through CliSwitchIntent so
        force_refresh and quiet apply on the ``--switch --json`` path."""
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home,
            sample_sequence_data,
        )
        live_state = {
            "creds": json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-live-1",
                        "refreshToken": "rt-live-1",
                    },
                }
            )
        }
        configs_store[("1", "test@example.com")] = json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": "test@example.com",
                    "accountUuid": "test-uuid-1234",
                },
            }
        )
        fresh_target_creds = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-fresh-2",
                    "refreshToken": "rt-fresh-2",
                    "expiresAt": 4_070_908_800_000,
                },
            }
        )
        patches = self._install_store_patches(
            switcher,
            creds_store,
            configs_store,
            live_state,
        )

        try:
            with (
                patch.object(
                    switcher,
                    "_refresh_target_credentials_before_activation",
                    wraps=switcher._refresh_target_credentials_before_activation,
                ) as spy,
                patch(
                    "claude_swap.oauth.refresh_oauth_credentials",
                    return_value=fresh_target_creds,
                ),
                patch.object(switcher, "list_accounts"),
            ):
                result = switcher.switch(json_output=True)
        finally:
            for p in patches:
                p.stop()

        assert result["switched"] is True
        spy.assert_called_once()
        assert spy.call_args.kwargs.get("force") is True
        assert capsys.readouterr().out == ""

    def test_activation_followup_text_is_platform_aware(self, temp_home: Path):
        """README documents the platform difference; the followup line must
        reflect it so the user-visible message stays honest with reality."""
        from claude_swap.models import Platform

        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.MACOS
        mac_text = switcher._activation_followup_text()
        assert "Keychain" in mac_text and "30s" in mac_text

        switcher.platform = Platform.LINUX
        linux_text = switcher._activation_followup_text()
        assert "next message" in linux_text
        assert "Keychain" not in linux_text

    def test_switch_followup_macos(self, temp_home: Path):
        """macOS shows the ~30s cache note; a restart applies it instantly."""
        switcher = ClaudeAccountSwitcher()
        switcher.platform = Platform.MACOS

        text = switcher._activation_followup_text()

        # Fork: _print_switch_followup replaced by platform-only _activation_followup_text.
        assert "30s" in text
        assert "Keychain" in text
        assert "next message" not in text

    @pytest.mark.parametrize(
        "plat", [Platform.LINUX, Platform.WSL, Platform.WINDOWS]
    )
    def test_switch_followup_non_macos(self, temp_home: Path, plat: Platform):
        """Linux/WSL/Windows show the immediate, no-restart note."""
        switcher = ClaudeAccountSwitcher()
        switcher.platform = plat

        text = switcher._activation_followup_text()

        assert "next message" in text, plat
        assert "30s" not in text, plat

    def test_write_credentials_verify_failure_aborts_switch(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """Defensive readback: when the storage layer silently returns
        different bytes than we wrote, ``_perform_switch`` must abort and
        roll back rather than commit a corrupt swap.

        Simulates the silent-Keychain-overwrite scenario by making
        ``_read_credentials`` return a stale payload after our write.
        """
        from claude_swap.exceptions import CredentialWriteError

        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home,
            sample_sequence_data,
        )
        live_state = {
            "creds": json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-live-1",
                        "refreshToken": "rt-live-1",
                    },
                }
            )
        }
        patches = self._install_store_patches(
            switcher,
            creds_store,
            configs_store,
            live_state,
        )

        # Inject a verify mismatch: _read_credentials returns a tampered
        # payload after the write, simulating a silent Keychain overwrite.
        def write_then_corrupt(creds, *, verify=False):
            live_state["creds"] = creds
            if verify:
                # Pretend readback returned something else entirely.
                raise CredentialWriteError(
                    "Credential write verification failed: readback differs "
                    "from intended payload."
                )

        try:
            with (
                patch.object(
                    switcher,
                    "_write_credentials",
                    side_effect=write_then_corrupt,
                ),
                patch(
                    "claude_swap.oauth.refresh_oauth_credentials",
                    return_value=creds_store[("2", "account2@example.com")],
                ),
            ):
                with pytest.raises(Exception) as exc_info:
                    switcher._perform_switch("2")
        finally:
            for p in patches:
                p.stop()

        # Either CredentialWriteError directly or SwitchError wrapping it.
        msg = str(exc_info.value)
        assert "verification failed" in msg or "readback" in msg

        # Sequence must NOT have advanced.
        data = switcher._get_sequence_data()
        assert data is not None
        assert data["activeAccountNumber"] == 1

    def test_multi_session_race_warning_logged_when_two_plus_running(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        caplog,
    ):
        """When >1 default-mode Claude Code processes are running, the
        switch must log a structured warning naming the PIDs and the
        underlying claude-code#24317 race condition. The switch still
        proceeds — the warning is informational."""
        import logging as _logging

        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home,
            sample_sequence_data,
        )
        live_state = {
            "creds": json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-live-1",
                        "refreshToken": "rt-live-1",
                    },
                }
            )
        }
        patches = self._install_store_patches(
            switcher,
            creds_store,
            configs_store,
            live_state,
        )

        caplog.set_level(_logging.WARNING, logger="claude-swap")
        try:
            with (
                patch.object(
                    switcher,
                    "_live_default_mode_claude_pids",
                    return_value=[101, 202, 303],
                ),
                patch(
                    "claude_swap.oauth.refresh_oauth_credentials",
                    return_value=creds_store[("2", "account2@example.com")],
                ),
                patch.object(switcher, "list_accounts"),
            ):
                switcher._perform_switch("2", intent=self._background_intent())
        finally:
            for p in patches:
                p.stop()

        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "claude-swap" and r.levelno == _logging.WARNING
        ]
        assert any(
            "multi-session race" in m and "101" in m and "303" in m and "24317" in m
            for m in warnings
        ), warnings

        # Switch still committed — the warning is non-blocking.
        data = switcher._get_sequence_data()
        assert data is not None
        assert data["activeAccountNumber"] == 2

    def test_multi_session_race_warning_silent_with_single_session(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        caplog,
    ):
        """With <=1 live Claude Code process the warning must not fire —
        log noise here would train users to ignore real signals."""
        import logging as _logging

        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        live_state = {
            "creds": json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-live-1",
                        "refreshToken": "rt-live-1",
                    },
                }
            )
        }
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        caplog.set_level(_logging.WARNING, logger="claude-swap")
        try:
            with (
                patch.object(
                    switcher,
                    "_live_default_mode_claude_pids",
                    return_value=[101],
                ),
                patch(
                    "claude_swap.oauth.refresh_oauth_credentials",
                    return_value=creds_store[("2", "account2@example.com")],
                ),
                patch.object(switcher, "list_accounts"),
            ):
                switcher._perform_switch("2", intent=self._background_intent())
        finally:
            for p in patches:
                p.stop()

        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == "claude-swap" and r.levelno == _logging.WARNING
        ]
        assert not any("multi-session race" in m for m in warnings), warnings

    def test_switch_survives_post_display_failure(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """Regression: a failure inside post-switch list_accounts() must not
        propagate as a switch failure. The swap already committed; the display
        is best-effort.
        """
        switcher, creds_store, configs_store = self._setup_two_accounts(
            temp_home, sample_sequence_data,
        )
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "sk-live-1",
                "refreshToken": "rt-live-1",
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            with (
                patch.object(
                    switcher,
                    "list_accounts",
                    side_effect=RuntimeError("boom"),
                ),
                patch(
                    "claude_swap.oauth.refresh_oauth_credentials",
                    return_value=creds_store[("2", "account2@example.com")],
                ),
            ):
                # Must not raise
                switcher._perform_switch("2")
        finally:
            for p in patches:
                p.stop()

        # Switch actually committed: sequence now points at account 2.
        data = switcher._get_sequence_data()
        assert data is not None
        assert data["activeAccountNumber"] == 2

        output = capsys.readouterr().out
        assert "Switched to" in output
        assert "usage display unavailable" in output
        # Followup line is platform-aware; both variants reference activation.
        assert "New account active" in output

    def test_switch_with_unset_active_account_does_not_write_none_backup(
        self,
        temp_home: Path,
        mock_claude_config: Path,
    ):
        """purge -> add-token -> switch-to must not back up live creds as None."""
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "target@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                }
            },
        })
        creds_store = {
            ("1", "target@example.com"): json.dumps({
                "claudeAiOauth": {
                    "accessToken": "target-token",
                    "refreshToken": None,
                    "expiresAt": None,
                    "scopes": ["user:inference"],
                    "subscriptionType": None,
                    "rateLimitTier": None,
                }
            }),
        }
        configs_store = {
            ("1", "target@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "target@example.com",
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }),
        }
        live_state = {"creds": json.dumps({
            "claudeAiOauth": {
                "accessToken": "existing-live-token",
                "refreshToken": "existing-refresh",
            },
        })}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            switcher._perform_switch("1")
        finally:
            for p in patches:
                p.stop()

        assert not any(num == "None" for num, _ in creds_store)
        assert not any(num == "None" for num, _ in configs_store)
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == (
            "target-token"
        )
        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1

    def test_switch_uses_live_identity_for_current_backup_slot(
        self,
        temp_home: Path,
    ):
        """Do not trust stale activeAccountNumber when backing up live creds."""
        config_path = temp_home / ".claude.json"
        config_path.write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "maintainer-b@example.com",
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        }))
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": 3,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [3, 4],
            "accounts": {
                "3": {
                    "email": "maintainer-a@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                },
                "4": {
                    "email": "maintainer-b@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                },
            },
        })
        target_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "target-token",
                "refreshToken": "target-refresh",
            }
        })
        live_creds = json.dumps({
            "claudeAiOauth": {
                "accessToken": "realiti-live-token",
                "refreshToken": "realiti-live-refresh",
            }
        })
        creds_store = {
            ("3", "maintainer-a@example.com"): target_creds,
            ("4", "maintainer-b@example.com"): "old-realiti-backup",
        }
        configs_store = {
            ("3", "maintainer-a@example.com"): json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "maintainer-a@example.com",
                        "accountUuid": "",
                        "organizationUuid": None,
                        "organizationName": None,
                    }
                }
            ),
            ("4", "maintainer-b@example.com"): "old-realiti-config",
        }
        live_state = {"creds": live_creds}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            with patch.object(switcher, "list_accounts"):
                switcher._perform_switch("3")
        finally:
            for p in patches:
                p.stop()

        assert creds_store[("4", "maintainer-b@example.com")] == live_creds
        assert ("3", "maintainer-b@example.com") not in creds_store
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == (
            "target-token"
        )

    def test_direct_activation_rolls_back_live_creds_on_sequence_write_failure(
        self,
        temp_home: Path,
    ):
        """Live creds must be restored if a write fails after they were swapped."""
        config_path = temp_home / ".claude.json"
        original_config_text = json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": "untracked@example.com",
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }
        )
        config_path.write_text(original_config_text)
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": None,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [1],
                "accounts": {
                    "1": {
                        "email": "target@example.com",
                        "uuid": "",
                        "organizationUuid": "",
                        "organizationName": "",
                        "added": "2024-01-01T00:00:00Z",
                    }
                },
            },
        )
        original_live_creds = json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "live-untracked-token",
                    "refreshToken": "live-untracked-refresh",
                }
            }
        )
        creds_store = {
            ("1", "target@example.com"): json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "target-token",
                        "refreshToken": "target-refresh",
                    }
                }
            ),
        }
        configs_store = {
            ("1", "target@example.com"): json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "target@example.com",
                        "accountUuid": "",
                        "organizationUuid": None,
                        "organizationName": None,
                    }
                }
            ),
        }
        live_state = {"creds": original_live_creds}
        patches = self._install_store_patches(
            switcher,
            creds_store,
            configs_store,
            live_state,
        )

        original_write_json = switcher._write_json

        def failing_write_json(path, data):
            if path == switcher.sequence_file and data.get("activeAccountNumber") == 1:
                raise OSError("disk full")
            return original_write_json(path, data)

        try:
            with (
                patch.object(
                    switcher,
                    "_write_json",
                    side_effect=failing_write_json,
                ),
                pytest.raises(OSError, match="disk full"),
            ):
                switcher._perform_switch("1")
        finally:
            for p in patches:
                p.stop()

        assert live_state["creds"] == original_live_creds
        assert config_path.read_text() == original_config_text

    def test_direct_activation_fresh_machine_removes_created_config_on_failure(
        self,
        temp_home: Path,
    ):
        """Fresh machine (no prior login): a config created during activation is
        removed if a later write fails, not left half-written."""
        config_path = temp_home / ".claude.json"
        assert not config_path.exists()  # truly fresh — no prior login
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(
            switcher.sequence_file,
            {
                "activeAccountNumber": None,
                "lastUpdated": "2024-01-01T00:00:00Z",
                "sequence": [1],
                "accounts": {
                    "1": {
                        "email": "target@example.com",
                        "uuid": "",
                        "organizationUuid": "",
                        "organizationName": "",
                        "added": "2024-01-01T00:00:00Z",
                    }
                },
            },
        )
        creds_store = {
            ("1", "target@example.com"): json.dumps({
                "claudeAiOauth": {
                    "accessToken": "target-token",
                    "refreshToken": "target-refresh",
                }
            }),
        }
        configs_store = {
            ("1", "target@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "target@example.com",
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }),
        }
        live_state = {"creds": None}  # no live login → fresh-machine path
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )
        original_write_json = switcher._write_json

        def failing_write_json(path, data):
            if path == switcher.sequence_file and data.get(
                "activeAccountNumber"
            ) == 1:
                raise OSError("disk full")
            return original_write_json(path, data)

        try:
            with patch.object(
                switcher, "_write_json", side_effect=failing_write_json,
            ), pytest.raises(OSError, match="disk full"):
                switcher._perform_switch("1")
        finally:
            for p in patches:
                p.stop()

        # The config we created on the fresh machine must be cleaned up.
        assert not config_path.exists()

    def test_direct_activation_fails_fast_when_live_creds_unreadable(
        self,
        temp_home: Path,
    ):
        """Refuse to overwrite live creds we couldn't snapshot for rollback."""
        config_path = temp_home / ".claude.json"
        original_config_text = json.dumps({
            "oauthAccount": {
                "emailAddress": "untracked@example.com",
                "accountUuid": "",
                "organizationUuid": None,
                "organizationName": None,
            }
        })
        config_path.write_text(original_config_text)
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._write_json(switcher.sequence_file, {
            "activeAccountNumber": None,
            "lastUpdated": "2024-01-01T00:00:00Z",
            "sequence": [1],
            "accounts": {
                "1": {
                    "email": "target@example.com",
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "2024-01-01T00:00:00Z",
                }
            },
        })
        creds_store = {
            ("1", "target@example.com"): json.dumps({
                "claudeAiOauth": {
                    "accessToken": "target-token",
                    "refreshToken": "target-refresh",
                }
            }),
        }
        configs_store = {
            ("1", "target@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "target@example.com",
                    "accountUuid": "",
                    "organizationUuid": None,
                    "organizationName": None,
                }
            }),
        }
        live_state = {"creds": "live-creds-that-we-cannot-read"}
        patches = self._install_store_patches(
            switcher, creds_store, configs_store, live_state,
        )

        try:
            with patch.object(
                switcher, "_read_credentials", return_value=None,
            ), pytest.raises(CredentialReadError, match="snapshot"):
                switcher._perform_switch("1")
        finally:
            for p in patches:
                p.stop()

        assert live_state["creds"] == "live-creds-that-we-cannot-read"
        assert config_path.read_text() == original_config_text


class TestSwitchToSelfSlotAndForce:
    """Issue #79: --switch-to onto the active account must not back up the
    live credentials into the target slot (destroying a freshly imported
    backup); --force is the explicit stored-backup → live recovery path."""

    _install_store_patches = staticmethod(
        TestPerformSwitchPostDisplay._install_store_patches
    )

    IMPORTED_1 = json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-imported-1",
            "refreshToken": "rt-imported-1",
        },
    })
    LIVE_1 = json.dumps({
        "claudeAiOauth": {
            "accessToken": "sk-live-1",
            "refreshToken": "rt-live-1",
        },
    })

    def _post_import_state(self, temp_home, sample_sequence_data):
        """Accounts 1 (active, live) & 2, with slot 1's stored backup holding
        freshly imported credentials that differ from the (stale) live ones."""
        sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher.platform = Platform.LINUX
        switcher._write_json(switcher.sequence_file, sample_sequence_data)

        (temp_home / ".claude" / ".credentials.json").write_text(self.LIVE_1)

        creds_store = {
            ("1", "test@example.com"): self.IMPORTED_1,
            ("2", "account2@example.com"): json.dumps({
                "claudeAiOauth": {
                    "accessToken": "sk-2",
                    "refreshToken": "rt-2",
                },
            }),
        }
        configs_store = {
            ("1", "test@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "test@example.com",
                    "accountUuid": "test-uuid-1234",
                },
            }),
            ("2", "account2@example.com"): json.dumps({
                "oauthAccount": {
                    "emailAddress": "account2@example.com",
                    "accountUuid": "uuid-2",
                },
            }),
        }
        live_state = {"creds": self.LIVE_1}
        return switcher, creds_store, configs_store, live_state

    def test_switch_to_current_slot_is_noop_preserving_backup(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """Human-mode self-switch neither poisons the stored backup nor
        rewrites the live credentials. Against main this fails: the switch
        backed up the live creds into slot 1 before reading them back."""
        switcher, creds, configs, live = self._post_import_state(
            temp_home, sample_sequence_data,
        )
        patches = self._install_store_patches(switcher, creds, configs, live)
        try:
            result = switcher.switch_to("1")
        finally:
            for p in patches:
                p.stop()

        assert result is None
        assert creds[("1", "test@example.com")] == self.IMPORTED_1
        assert live["creds"] == self.LIVE_1
        out = capsys.readouterr().out
        assert "Already on" in out and "Account-1" in out
        assert "cswap --switch-to 1 --force" in out

    def test_force_self_activation_restores_imported_creds(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """--switch-to 1 --force rewrites the live login from the stored
        backup without backing up the stale live creds first."""
        switcher, creds, configs, live = self._post_import_state(
            temp_home, sample_sequence_data,
        )
        patches = self._install_store_patches(switcher, creds, configs, live)
        try:
            result = switcher.switch_to("1", force=True)
        finally:
            for p in patches:
                p.stop()

        assert result is None
        assert live["creds"] == self.IMPORTED_1
        assert creds[("1", "test@example.com")] == self.IMPORTED_1
        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 1
        assert "Activated" in capsys.readouterr().out

    def test_force_cross_slot_skips_backup_of_current(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """--switch-to 2 --force lands on account 2 without writing the stale
        live creds into slot 1's freshly imported backup."""
        switcher, creds, configs, live = self._post_import_state(
            temp_home, sample_sequence_data,
        )
        patches = self._install_store_patches(switcher, creds, configs, live)
        try:
            switcher.switch_to("2", force=True)
        finally:
            for p in patches:
                p.stop()

        assert creds[("1", "test@example.com")] == self.IMPORTED_1
        assert json.loads(live["creds"])["claudeAiOauth"]["accessToken"] == "sk-2"
        data = switcher._get_sequence_data()
        assert data["activeAccountNumber"] == 2


# ── Task 1: AccountInfo org fields ───────────────────────────────────────────

