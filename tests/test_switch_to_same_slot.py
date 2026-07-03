"""Boundary tests for the same-slot ``--switch-to`` no-op (upstream #79).

Upstream's fix (acb0644) makes a self-switch a no-op in both modes and adds
``--force`` as the explicit stored-backup → live recovery path; its core
behavior is pinned by ``TestSwitchToSelfSlotAndForce`` in test_switcher.py.
This module keeps only the boundaries upstream does not cover: the guard
short-circuits *before* ``_perform_switch``, keys off the live identity (not
a drifted ``activeAccountNumber``), and never swallows a genuine cross-slot
switch.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher

GOOD_SLOT_CREDS = json.dumps(
    {"claudeAiOauth": {"accessToken": "sk-good", "refreshToken": "rt-good"}}
)
BAD_LIVE_CREDS = json.dumps(
    {"claudeAiOauth": {"accessToken": "sk-bad", "refreshToken": "rt-bad"}}
)


def _post_import_switcher(temp_home: Path, sample_sequence_data: dict):
    """Switcher where slot 1 holds fresh creds but live still holds stale ones."""
    sample_sequence_data["accounts"]["1"]["email"] = "test@example.com"
    switcher = ClaudeAccountSwitcher()
    switcher._setup_directories()
    switcher.platform = Platform.LINUX
    switcher._write_json(switcher.sequence_file, sample_sequence_data)

    (temp_home / ".claude" / ".credentials.json").write_text(BAD_LIVE_CREDS)

    creds_store = {
        ("1", "test@example.com"): GOOD_SLOT_CREDS,
        ("2", "account2@example.com"): json.dumps(
            {"claudeAiOauth": {"accessToken": "sk-2", "refreshToken": "rt-2"}}
        ),
    }
    configs_store = {
        ("1", "test@example.com"): json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": "test@example.com",
                    "accountUuid": "test-uuid-1234",
                }
            }
        ),
        ("2", "account2@example.com"): json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": "account2@example.com",
                    "accountUuid": "uuid-2",
                }
            }
        ),
    }
    live_state = {"creds": BAD_LIVE_CREDS}
    patches = [
        patch.object(
            switcher,
            "_read_account_credentials",
            side_effect=lambda n, e: creds_store.get((str(n), e), ""),
        ),
        patch.object(
            switcher,
            "_write_account_credentials",
            side_effect=lambda n, e, c: creds_store.__setitem__((str(n), e), c),
        ),
        patch.object(
            switcher,
            "_read_account_config",
            side_effect=lambda n, e: configs_store.get((str(n), e), ""),
        ),
        patch.object(
            switcher,
            "_write_account_config",
            side_effect=lambda n, e, c: configs_store.__setitem__((str(n), e), c),
        ),
        patch.object(
            switcher,
            "_read_credentials",
            side_effect=lambda: live_state.get("creds", ""),
        ),
        patch.object(
            switcher,
            "_write_credentials",
            side_effect=lambda c, verify=False: live_state.__setitem__("creds", c),
        ),
        patch("claude_swap.oauth.fetch_usage_for_account", return_value=None),
    ]
    return switcher, creds_store, live_state, patches


class TestHumanModeSameSlotNoop:
    def test_same_slot_short_circuits_before_perform_switch(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        switcher, _creds, _live, patches = _post_import_switcher(
            temp_home, sample_sequence_data
        )
        for p in patches:
            p.start()
        try:
            with patch.object(switcher, "_perform_switch") as perform:
                switcher.switch_to("1", json_output=False)
        finally:
            for p in patches:
                p.stop()
        perform.assert_not_called()

    def test_sequence_drift_still_noops_on_live_identity(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
        capsys,
    ):
        """activeAccountNumber drift must not defeat the identity-based no-op.

        The short-circuit keys off the live oauthAccount identity, not the
        possibly stale activeAccountNumber in sequence.json.
        """
        sample_sequence_data["activeAccountNumber"] = 2
        switcher, creds_store, live_state, patches = _post_import_switcher(
            temp_home, sample_sequence_data
        )
        for p in patches:
            p.start()
        try:
            result = switcher.switch_to("1", json_output=False)
        finally:
            for p in patches:
                p.stop()

        assert result is None
        assert creds_store[("1", "test@example.com")] == GOOD_SLOT_CREDS
        assert live_state["creds"] == BAD_LIVE_CREDS
        out = capsys.readouterr().out
        assert "Already on" in out and "Account-1" in out
        assert "cswap --switch-to 1 --force" in out

    def test_different_slot_still_performs_full_swap(
        self,
        temp_home: Path,
        mock_claude_config: Path,
        sample_sequence_data: dict,
    ):
        """The no-op must not swallow genuine cross-slot switches."""
        switcher, creds_store, live_state, patches = _post_import_switcher(
            temp_home, sample_sequence_data
        )
        for p in patches:
            p.start()
        try:
            with patch.object(switcher, "list_accounts"):
                switcher.switch_to("2", json_output=False)
        finally:
            for p in patches:
                p.stop()

        # Live credentials were backed up into slot 1, slot 2 went live.
        assert creds_store[("1", "test@example.com")] == BAD_LIVE_CREDS
        assert json.loads(live_state["creds"])["claudeAiOauth"]["accessToken"] == "sk-2"
