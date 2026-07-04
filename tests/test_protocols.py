"""Structural Protocol satisfaction for credential refresh and switch CLI hosts."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from claude_swap.credential_refresh import CredentialRefresher
from claude_swap.models import (
    SwitchPreconditionKind,
    SwitchPreconditions,
)
from claude_swap.switch_cli import run_switch_cli


def test_credential_refresher_accepts_minimal_refresh_host(tmp_path: Path):
    lock_file = tmp_path / ".lock"
    store: dict[tuple[str, str], str] = {}
    live = {
        "creds": '{"claudeAiOauth":{"accessToken":"t","refreshToken":"r","expiresAt":9}}'
    }
    host = SimpleNamespace(
        lock_file=lock_file,
        _logger=logging.getLogger("test.refresh_host"),
        _read_credentials=lambda: live["creds"],
        _read_account_credentials=lambda num, email: store.get((num, email), ""),
        _write_account_credentials=lambda num, email, creds: store.__setitem__(
            (num, email),
            creds,
        ),
        _live_session_pids=lambda num, email: [],
    )
    refresher = CredentialRefresher(host)
    result = refresher.write_verified_live("1", "a@example.com", live["creds"])
    assert result == live["creds"]


def test_run_switch_cli_accepts_minimal_switch_cli_host():
    host = SimpleNamespace(
        _classify_switch_preconditions=lambda: SwitchPreconditions(
            kind=SwitchPreconditionKind.SINGLE_ACCOUNT,
            identity=("a@example.com", "org-uuid"),
            current_slot="1",
        ),
        _switch_noop=lambda **kwargs: {
            "switched": False,
            "strategy": kwargs["strategy"],
            "reason": kwargs["reason"],
        },
    )
    result = run_switch_cli(host, json_output=True)
    assert result == {
        "switched": False,
        "strategy": "rotation",
        "reason": "only-one-account",
    }
