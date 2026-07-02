"""Gates that keep the live→backup credential sync from poisoning backups.

The list/status paths opportunistically sync the active account's live
credentials into its backup slot (upstream #70: keep Claude-Code-rotated
tokens). Two ways that sync can destroy good backups are pinned here:

1. macOS, Keychain transiently locked: the active read falls back to the
   plaintext ``.credentials.json``, which Keychain-mode writes deliberately
   leave untouched across switches (#1414) — so it may hold ANOTHER account's
   stale credentials. Syncing that read poisons the active slot's backup with
   a different account's tokens (adversarial review R2, finding B1).
2. ``--import --force`` onto the ACTIVE slot: import only writes the backup,
   so the next list/status sync would overwrite the freshly imported backup
   with the stale live credentials — defeating the same-slot no-op protection
   added for upstream #79 (R2, finding M1).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from unittest.mock import patch

from claude_swap import macos_keychain
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher

from tests.conftest import raise_locked as _raise_locked


def _oauth_creds(tag: str, *, expires_in_s: int = 3600) -> str:
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": f"at-{tag}",
                "refreshToken": f"rt-{tag}",
                "expiresAt": int(time.time() * 1000) + expires_in_s * 1000,
                "scopes": ["user:inference"],
            }
        }
    )


def _seed_switcher(
    temp_home: Path,
    accounts: dict[str, str],
    active: int,
    live_email: str,
) -> ClaudeAccountSwitcher:
    sw = ClaudeAccountSwitcher()
    sw._setup_directories()
    sw._write_json(
        sw.sequence_file,
        {
            "activeAccountNumber": active,
            "lastUpdated": "2026-01-01T00:00:00Z",
            "sequence": [int(n) for n in accounts],
            "accounts": {
                n: {
                    "email": e,
                    "uuid": "",
                    "organizationUuid": "",
                    "organizationName": "",
                    "added": "",
                }
                for n, e in accounts.items()
            },
        },
    )
    (temp_home / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": live_email,
                    "organizationUuid": "",
                    "accountUuid": "u",
                }
            }
        )
    )
    return sw


class TestDegradedActiveReadNeverSyncs:
    """R2-B1: a Keychain-failure file fallback must not reach the backup sync."""

    def _locked_keychain_switcher(
        self, temp_home: Path, monkeypatch
    ) -> ClaudeAccountSwitcher:
        sw = _seed_switcher(
            temp_home,
            {"1": "alice@example.com", "2": "bob@example.com"},
            active=2,
            live_email="bob@example.com",
        )
        sw.platform = Platform.MACOS
        monkeypatch.setattr(macos_keychain, "get_password", _raise_locked)
        monkeypatch.setattr(macos_keychain, "set_password", _raise_locked)
        monkeypatch.setattr(macos_keychain, "delete_password", _raise_locked)
        monkeypatch.setattr("claude_swap.credentials._ACTIVE_READ_RETRY_DELAY", 0)
        return sw

    def test_status_does_not_sync_other_accounts_stale_file(
        self, temp_home: Path, monkeypatch, capsys
    ):
        """Locked Keychain + leftover file of another account: backup survives.

        Alice's stale file carries a NEWER expiresAt than bob's backup, so a
        freshness gate alone would not stop the overwrite — only the degraded
        classification of the read does.
        """
        sw = self._locked_keychain_switcher(temp_home, monkeypatch)
        alice_stale = _oauth_creds("alice-old", expires_in_s=7200)
        bob_good = _oauth_creds("bob-good", expires_in_s=3600)
        (temp_home / ".claude" / ".credentials.json").write_text(alice_stale)
        sw._write_account_credentials("2", "bob@example.com", bob_good)

        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            sw.status()

        assert sw._read_account_credentials("2", "bob@example.com") == bob_good
        capsys.readouterr()

    def test_list_does_not_sync_other_accounts_stale_file(
        self, temp_home: Path, monkeypatch, capsys
    ):
        sw = self._locked_keychain_switcher(temp_home, monkeypatch)
        alice_stale = _oauth_creds("alice-old", expires_in_s=7200)
        bob_good = _oauth_creds("bob-good", expires_in_s=3600)
        (temp_home / ".claude" / ".credentials.json").write_text(alice_stale)
        sw._write_account_credentials("2", "bob@example.com", bob_good)

        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            sw.list_accounts()

        assert sw._read_account_credentials("2", "bob@example.com") == bob_good
        capsys.readouterr()

    def test_absent_keychain_item_file_fallback_still_syncs(
        self, temp_home: Path, capsys
    ):
        """The legit file fallback (item absent, rc-44 — headless / file-mode
        macOS logins, #60/#66) is NOT degraded and must keep syncing (#70)."""
        sw = _seed_switcher(
            temp_home,
            {"1": "user@example.com"},
            active=1,
            live_email="user@example.com",
        )
        sw.platform = Platform.MACOS
        live_rotated = _oauth_creds("rotated", expires_in_s=7200)
        old_backup = _oauth_creds("old", expires_in_s=60)
        (temp_home / ".claude" / ".credentials.json").write_text(live_rotated)
        sw._write_account_credentials("1", "user@example.com", old_backup)

        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            sw.status()

        stored = sw._read_account_credentials("1", "user@example.com")
        assert json.loads(stored)["claudeAiOauth"]["refreshToken"] == "rt-rotated"
        capsys.readouterr()

    def test_degraded_read_never_consumes_refresh_token(
        self, temp_home: Path, monkeypatch, capsys, caplog
    ):
        """Usage display may use a degraded read, but must not refresh with it.

        Refreshing would consume the file owner's single-use refresh token and
        try to persist the rotation into the active slot's live/backup stores.
        """
        sw = self._locked_keychain_switcher(temp_home, monkeypatch)
        alice_stale = _oauth_creds("alice-old", expires_in_s=-60)
        (temp_home / ".claude" / ".credentials.json").write_text(alice_stale)

        refresh_calls: list[str] = []

        def no_refresh(creds: str) -> str:
            refresh_calls.append(creds)
            return _oauth_creds("rotated")

        monkeypatch.setattr(
            "claude_swap.oauth.refresh_oauth_credentials", no_refresh
        )
        with (
            patch("claude_swap.oauth.request_usage_data", side_effect=OSError("no net")),
            caplog.at_level(logging.DEBUG, logger="claude-swap"),
        ):
            sw.status()

        assert refresh_calls == []
        assert sw._read_account_credentials("2", "bob@example.com") == ""
        capsys.readouterr()


class TestSyncFreshnessGate:
    """R2-M1: live→backup sync must not undo `--import --force` on the active slot."""

    def _import_over_active(self, temp_home: Path) -> tuple[ClaudeAccountSwitcher, str, str]:
        sw = _seed_switcher(
            temp_home,
            {"1": "user@example.com"},
            active=1,
            live_email="user@example.com",
        )
        sw.platform = Platform.LINUX
        live_old = _oauth_creds("live-old", expires_in_s=600)
        (temp_home / ".claude" / ".credentials.json").write_text(live_old)
        # `--import --force` writes only the backup slot, never the live store.
        imported = _oauth_creds("imported-fresh", expires_in_s=7200)
        sw._write_account_credentials("1", "user@example.com", imported)
        return sw, live_old, imported

    def test_status_keeps_freshly_imported_backup(self, temp_home: Path, capsys):
        sw, _live_old, imported = self._import_over_active(temp_home)

        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            sw.status()

        assert sw._read_account_credentials("1", "user@example.com") == imported
        capsys.readouterr()

    def test_list_keeps_freshly_imported_backup(self, temp_home: Path, capsys):
        sw, _live_old, imported = self._import_over_active(temp_home)

        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            sw.list_accounts()

        assert sw._read_account_credentials("1", "user@example.com") == imported
        capsys.readouterr()

    def test_rotated_live_token_still_syncs_over_older_backup(
        self, temp_home: Path, capsys
    ):
        """PR#70 semantics: a genuinely rotated live token (newer than the
        backup) must keep flowing into the backup slot."""
        sw = _seed_switcher(
            temp_home,
            {"1": "user@example.com"},
            active=1,
            live_email="user@example.com",
        )
        sw.platform = Platform.LINUX
        old_backup = _oauth_creds("old", expires_in_s=60)
        live_rotated = _oauth_creds("rotated", expires_in_s=7200)
        sw._write_account_credentials("1", "user@example.com", old_backup)
        (temp_home / ".claude" / ".credentials.json").write_text(live_rotated)

        with patch("claude_swap.oauth.fetch_usage_for_account", return_value=None):
            sw.status()

        stored = sw._read_account_credentials("1", "user@example.com")
        assert json.loads(stored)["claudeAiOauth"]["refreshToken"] == "rt-rotated"
        capsys.readouterr()
