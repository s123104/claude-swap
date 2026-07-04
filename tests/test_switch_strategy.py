"""Usage-aware switch strategy, scoring contract, and auto-switch guards."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


class TestClassifySwitchPreconditions:
    """Focused coverage for _classify_switch_preconditions()."""

    def _setup(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _seed(
        self,
        s: ClaudeAccountSwitcher,
        num: int,
        email: str,
        *,
        creds: bool = True,
        config: bool = True,
    ) -> None:
        if creds:
            s._write_account_credentials(
                str(num),
                email,
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": f"sk-{num}",
                            "refreshToken": f"rt-{num}",
                        },
                    }
                ),
            )
        if config:
            s._write_account_config(
                str(num),
                email,
                json.dumps(
                    {
                        "oauthAccount": {
                            "emailAddress": email,
                            "accountUuid": f"uuid-{num}",
                        },
                    }
                ),
            )

        data = s._get_sequence_data() or {
            "activeAccountNumber": None,
            "lastUpdated": "",
            "sequence": [],
            "accounts": {},
        }
        data["accounts"][str(num)] = {
            "email": email,
            "uuid": f"uuid-{num}",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        s._write_json(s.sequence_file, data)

    def _set_live_identity(
        self, temp_home: Path, email: str, uuid: str = "uuid-1"
    ) -> None:
        (temp_home / ".claude").mkdir(parents=True, exist_ok=True)
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": email,
                        "accountUuid": uuid,
                    },
                }
            )
        )

    def test_fresh_machine(self, temp_home: Path):
        from claude_swap.models import SwitchPreconditionKind

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")

        result = s._classify_switch_preconditions()

        assert result.kind == SwitchPreconditionKind.FRESH_MACHINE
        assert result.identity is None
        assert result.data is None
        assert result.sequence is None
        assert result.current_slot is None

    def test_unmanaged(self, temp_home: Path):
        from claude_swap.models import SwitchPreconditionKind

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._set_live_identity(temp_home, "unknown@example.com", "uuid-x")

        result = s._classify_switch_preconditions()

        assert result.kind == SwitchPreconditionKind.UNMANAGED
        assert result.identity == ("unknown@example.com", "")
        assert result.data is None
        assert result.sequence is None
        assert result.current_slot is None

    def test_single_account(self, temp_home: Path):
        from claude_swap.models import SwitchPreconditionKind

        s = self._setup(temp_home)
        self._seed(s, 1, "solo@example.com")
        self._set_live_identity(temp_home, "solo@example.com", "uuid-1")

        result = s._classify_switch_preconditions()

        assert result.kind == SwitchPreconditionKind.SINGLE_ACCOUNT
        assert result.identity == ("solo@example.com", "")
        assert result.data is not None
        assert result.sequence == [1]
        assert result.current_slot == "1"

    def test_ready(self, temp_home: Path):
        from claude_swap.models import SwitchPreconditionKind

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._set_live_identity(temp_home, "a@example.com", "uuid-1")

        result = s._classify_switch_preconditions()

        assert result.kind == SwitchPreconditionKind.READY
        assert result.identity == ("a@example.com", "")
        assert result.data is not None
        assert result.sequence == [1, 2]
        assert result.current_slot is None

    def test_missing_sequence_data_degrades_cleanly(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A missing/corrupt sequence.json makes _get_sequence_data return None;
        # the classifier must treat it as empty (no accounts), not raise
        # AttributeError on None.get(...).
        from claude_swap.models import SwitchPreconditionKind

        s = self._setup(temp_home)
        self._set_live_identity(temp_home, "a@example.com", "uuid-1")
        monkeypatch.setattr(
            s, "_switch_bootstrap_identity", lambda: ("a@example.com", "")
        )
        monkeypatch.setattr(s, "_account_exists", lambda email, org: True)
        # Corrupt/missing sequence.json -> _get_sequence_data returns None; the
        # `or {}` guard must let the classifier reach a clean verdict instead of
        # raising AttributeError on None.get("sequence").
        monkeypatch.setattr(s, "_get_sequence_data", lambda: None)

        result = s._classify_switch_preconditions()

        assert result.kind == SwitchPreconditionKind.SINGLE_ACCOUNT


class TestUsageAwareSwitch:
    """--switch --strategy best / next-available pick targets by remaining 5h/7d
    quota. `best` only switches when another account is provably better and
    otherwise stays put; `next-available` rotates, skipping accounts at their
    limit (and anchors on the live account)."""

    def _setup(self, temp_home: Path) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _seed(self, s: ClaudeAccountSwitcher, num: int, email: str) -> None:
        s._write_account_credentials(
            str(num),
            email,
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": f"sk-{num}",
                        "refreshToken": f"rt-{num}",
                    },
                }
            ),
        )
        s._write_account_config(
            str(num),
            email,
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": email,
                        "accountUuid": f"uuid-{num}",
                    },
                }
            ),
        )
        data = s._get_sequence_data()
        data["accounts"][str(num)] = {
            "email": email,
            "uuid": f"uuid-{num}",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if data["activeAccountNumber"] is None:
            data["activeAccountNumber"] = num
        s._write_json(s.sequence_file, data)

    def _make_live(self, temp_home: Path, email: str, num: int) -> None:
        (temp_home / ".claude" / ".credentials.json").write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "sk-live",
                        "refreshToken": "rt-live",
                    },
                }
            )
        )
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": email,
                        "accountUuid": f"uuid-{num}",
                    },
                }
            )
        )

    @staticmethod
    def _usage(pct: float) -> dict:
        return {"five_hour": {"pct": pct}, "seven_day": {"pct": 0.0}}

    def test_best_switches_to_more_headroom(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(50), "2": self._usage(90), "3": self._usage(20)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts"),
        ):
            s.switch(strategy="best")

        assert s._get_sequence_data()["activeAccountNumber"] == 3

    def test_best_stays_when_current_is_already_best(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(89), "2": self._usage(100)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts") as mock_list,
        ):
            s.switch(strategy="best")

        assert (
            "Already on the account with the most remaining quota"
            in capsys.readouterr().out
        )
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_best_all_exhausted_stays_put(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(100), "2": self._usage(100), "3": self._usage(100)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts"),
        ):
            s.switch(strategy="best")

        out = capsys.readouterr().out
        assert "All accounts are at their 5h/7d limit" in out
        assert "staying on Account-1" in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1

    def test_best_current_usage_unavailable_stays(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": None, "2": self._usage(10)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts") as mock_list,
        ):
            s.switch(strategy="best")

        assert "Current account usage is unavailable" in capsys.readouterr().out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_best_no_candidate_usage_stays(self, temp_home: Path, capsys):
        """Current known but no other account has usage data → no comparison is
        possible → stay (not rotation, not 'all exhausted')."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(50), "2": None}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts") as mock_list,
        ):
            s.switch(strategy="best")

        out = capsys.readouterr().out
        assert "No other account has usage data to compare" in out
        assert "All accounts are at their 5h/7d limit" not in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_best_incomplete_comparison_stays(self, temp_home: Path, capsys):
        """Current known + a known *worse* candidate + an unknown candidate →
        stay, without claiming 'most remaining quota' or 'all exhausted' (the
        unknown one can't be ruled better)."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(50), "2": self._usage(90), "3": None}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts") as mock_list,
        ):
            s.switch(strategy="best")

        out = capsys.readouterr().out
        assert "some usage is unavailable" in out
        assert "most remaining quota" not in out
        assert "All accounts are at their 5h/7d limit" not in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_best_current_exhausted_with_unknown_candidate_stays(
        self, temp_home: Path, capsys
    ):
        """Current known & exhausted + a known (also-exhausted) candidate + an
        unknown candidate → stay, but must NOT claim 'all exhausted' since the
        unknown account might have room."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(100), "2": self._usage(100), "3": None}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts") as mock_list,
        ):
            s.switch(strategy="best")

        out = capsys.readouterr().out
        assert "some usage is unavailable" in out
        assert "All accounts are at their 5h/7d limit" not in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_skip_exhausted_skips_limited_account(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(0), "2": self._usage(100), "3": self._usage(20)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts"),
        ):
            s.switch(strategy="next-available")

        out = capsys.readouterr().out
        assert "Skipping Account-2 (at 5h/7d limit)" in out
        assert s._get_sequence_data()["activeAccountNumber"] == 3

    def test_skip_exhausted_all_limited_stays_put(self, temp_home: Path, capsys):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        usage = {"1": self._usage(0), "2": self._usage(100), "3": self._usage(100)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts") as mock_list,
        ):
            s.switch(strategy="next-available")

        out = capsys.readouterr().out
        assert "staying on Account-1" in out
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        mock_list.assert_not_called()

    def test_skip_exhausted_unknown_usage_is_not_skipped(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        with (
            patch.object(s, "_usage_by_account", return_value={"1": None, "2": None}),
            patch.object(s, "list_accounts"),
        ):
            s.switch(strategy="next-available")

        assert s._get_sequence_data()["activeAccountNumber"] == 2

    def test_next_available_anchors_on_live_account_under_drift(self, temp_home: Path):
        """When the live login has drifted from activeAccountNumber,
        next-available rotates relative to the LIVE account (current_num), not
        the stale record — so it never no-ops onto the account you're already
        on."""
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._seed(s, 3, "c@example.com")
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 1
        s._write_json(s.sequence_file, data)
        self._make_live(temp_home, "b@example.com", 2)

        usage = {"1": self._usage(0), "2": self._usage(0), "3": self._usage(0)}
        with (
            patch.object(s, "_usage_by_account", return_value=usage),
            patch.object(s, "list_accounts"),
        ):
            s.switch(strategy="next-available")

        assert s._get_sequence_data()["activeAccountNumber"] == 3


class TestClaudeCodeLockCooperation:
    """_perform_switch must hold Claude Code's own advisory locks
    (~/.claude.lock and ~/.claude.json.lock) while mutating credentials/config,
    and fail cleanly — before any mutation — when Claude Code holds them."""

    _setup = TestUsageAwareSwitch._setup
    _seed = TestUsageAwareSwitch._seed
    _make_live = TestUsageAwareSwitch._make_live

    def test_switch_holds_both_cc_locks_at_write_time(self, temp_home: Path):
        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        creds_lock = temp_home / ".claude.lock"
        config_lock = temp_home / ".claude.json.lock"
        seen: list[tuple[bool, bool]] = []
        original_write = s._write_credentials

        def spying_write(credentials: str, *, verify: bool = False) -> None:
            seen.append((creds_lock.is_dir(), config_lock.is_dir()))
            original_write(credentials, verify=verify)

        with patch.object(s, "_write_credentials", side_effect=spying_write), \
             patch.object(s, "list_accounts"):
            s.switch_to("2")

        assert s._get_sequence_data()["activeAccountNumber"] == 2
        assert seen and all(pair == (True, True) for pair in seen)
        # Released after the switch.
        assert not creds_lock.exists()
        assert not config_lock.exists()

    def test_preheld_cc_lock_fails_cleanly_without_mutation(
        self, temp_home: Path, monkeypatch
    ):
        from claude_swap import claude_locks
        from claude_swap.exceptions import ClaudeCodeLockTimeout

        s = self._setup(temp_home)
        self._seed(s, 1, "a@example.com")
        self._seed(s, 2, "b@example.com")
        self._make_live(temp_home, "a@example.com", 1)

        monkeypatch.setattr(claude_locks, "DEFAULT_TIMEOUT_S", 0.3)
        (temp_home / ".claude.lock").mkdir()  # fresh mtime = live CC refresh

        live_creds_before = (
            temp_home / ".claude" / ".credentials.json"
        ).read_text()
        with pytest.raises(ClaudeCodeLockTimeout):
            s.switch_to("2")

        # Nothing was mutated: locks acquire before any write.
        assert s._get_sequence_data()["activeAccountNumber"] == 1
        live_creds_after = (
            temp_home / ".claude" / ".credentials.json"
        ).read_text()
        assert live_creds_after == live_creds_before
        # The holder's lock was left alone.
        assert (temp_home / ".claude.lock").is_dir()


class TestSwitchGuards:
    """Interactive callers see friendly print + return for "nothing to switch
    to" cases. The engine never reaches switch() in these states: it inspects
    candidates itself and emits a no-switch event instead.
    """

    def _bootstrap(self, temp_home: Path, num_accounts: int) -> ClaudeAccountSwitcher:
        s = ClaudeAccountSwitcher()
        s._setup_directories()
        accounts: dict = {}
        sequence: list[int] = []
        for i in range(1, num_accounts + 1):
            accounts[str(i)] = {"email": f"a{i}@example.com"}
            sequence.append(i)
        data = {
            "accounts": accounts,
            "sequence": sequence,
            "activeAccountNumber": 1 if sequence else None,
        }
        s._write_json(s.sequence_file, data)
        # Pretend there's a current login for account 1 so we get past
        # the fresh-machine path and into the len(sequence)<2 guard.
        (temp_home / ".claude").mkdir(parents=True, exist_ok=True)
        (temp_home / ".claude.json").write_text(
            json.dumps(
                {
                    "oauthAccount": {
                        "emailAddress": "a1@example.com",
                        "accountUuid": "uuid-1",
                    }
                }
            )
        )
        return s

    def test_interactive_single_account_prints_and_returns(
        self, temp_home: Path, capsys
    ):
        s = self._bootstrap(temp_home, num_accounts=1)
        s.switch()
        out = capsys.readouterr().out
        assert "Only one account" in out
