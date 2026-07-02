"""Rollback failure paths for ``--import`` (transfer.py).

Adversarial review R2, nit: the import rollback branches — undoing a
half-written entry and unwinding previously completed entries when a later
one fails — carried almost no coverage despite being the error handling that
keeps a failed multi-account import from leaving the store half-migrated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_swap.exceptions import TransferError
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.transfer import import_accounts

from tests.test_transfer import SAMPLE_CONFIG, _linux_switcher, _seed_account


def _entry(email: str, number: int, marker: str) -> dict:
    config = json.loads(json.dumps(SAMPLE_CONFIG))
    config["oauthAccount"]["emailAddress"] = email
    return {
        "number": number,
        "email": email,
        "uuid": f"u-{number}",
        "organizationUuid": "",
        "organizationName": "",
        "added": "2024-01-01T00:00:00Z",
        "credentials": {"accessToken": "tok", "_marker": marker},
        "config": config,
    }


def _write_envelope(path: Path, *entries: dict) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "exportedAt": "2026-01-01T00:00:00Z",
                "exportedFrom": "linux",
                "swapVersion": "0.0.0",
                "encrypted": False,
                "activeAccountNumber": None,
                "accounts": list(entries),
            }
        )
    )


class TestImportRollback:
    def test_mid_import_failure_rolls_back_completed_entries(
        self, temp_home: Path, capsys
    ):
        """A failure on entry N unwinds entries 1..N-1 imported by this run."""
        s = _linux_switcher(temp_home)
        envelope = temp_home / "two.cswap"
        _write_envelope(
            envelope,
            _entry("alice@example.com", 1, "ALICE"),
            _entry("bob@example.com", 2, "BOB"),
        )

        real_write = ClaudeAccountSwitcher._write_account_credentials

        def failing_second_write(self, num, email, creds):
            if email == "bob@example.com":
                raise OSError("disk full")
            return real_write(self, num, email, creds)

        with (
            pytest.MonkeyPatch.context() as mp,
        ):
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_credentials",
                failing_second_write,
            )
            with pytest.raises(TransferError, match="rolled back 1 account"):
                import_accounts(s, str(envelope))

        # Alice's completed import was fully unwound: no slot record, no files.
        seq = s._get_sequence_data() or {}
        assert "1" not in seq.get("accounts", {})
        assert s._read_account_credentials("1", "alice@example.com") == ""
        assert s._read_account_config("1", "alice@example.com") == ""
        capsys.readouterr()

    def test_overwrite_failure_restores_previous_slot_contents(
        self, temp_home: Path, capsys
    ):
        """A half-done --force overwrite restores the pre-import slot state."""
        s = _linux_switcher(temp_home)
        _seed_account(s, 1, "alice@example.com")
        creds_before = s._read_account_credentials("1", "alice@example.com")
        config_before = s._read_account_config("1", "alice@example.com")

        envelope = temp_home / "force.cswap"
        _write_envelope(envelope, _entry("alice@example.com", 1, "ALICE-NEW"))

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_config",
                lambda self, num, email, cfg: (_ for _ in ()).throw(
                    OSError("config write failed")
                ),
            )
            with pytest.raises(TransferError, match="import failed on alice"):
                import_accounts(s, str(envelope), force=True)

        assert s._read_account_credentials("1", "alice@example.com") == creds_before
        assert s._read_account_config("1", "alice@example.com") == config_before
        seq = s._get_sequence_data() or {}
        assert seq["accounts"]["1"]["email"] == "alice@example.com"
        capsys.readouterr()

    def test_rollback_failure_names_unrollable_accounts(
        self, temp_home: Path, capsys
    ):
        """When the rollback itself fails, the error says what was kept."""
        s = _linux_switcher(temp_home)
        envelope = temp_home / "two.cswap"
        _write_envelope(
            envelope,
            _entry("alice@example.com", 1, "ALICE"),
            _entry("bob@example.com", 2, "BOB"),
        )

        real_write = ClaudeAccountSwitcher._write_account_credentials
        real_delete = ClaudeAccountSwitcher._delete_account_credentials

        def failing_second_write(self, num, email, creds):
            if email == "bob@example.com":
                raise OSError("disk full")
            return real_write(self, num, email, creds)

        def failing_delete(self, num, email):
            if email == "alice@example.com":
                raise OSError("delete failed")
            return real_delete(self, num, email)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                ClaudeAccountSwitcher,
                "_write_account_credentials",
                failing_second_write,
            )
            mp.setattr(
                ClaudeAccountSwitcher,
                "_delete_account_credentials",
                failing_delete,
            )
            with pytest.raises(
                TransferError, match="could not roll back alice@example.com"
            ):
                import_accounts(s, str(envelope))
        capsys.readouterr()
