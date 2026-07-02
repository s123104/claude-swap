"""Tests for the supervised (service-mode) monitor invocation.

Service backends pass ``--service-monitor`` on the supervised argv so the
monitor knows a service manager is watching it. Task Scheduler has no
per-task environment variables (its XML schema rejects an
``EnvironmentVariables`` node under ``Exec``), so an argv flag is the only
channel that works uniformly across launchd, systemd and Task Scheduler.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, monitor
from claude_swap.switcher import ClaudeAccountSwitcher


def _write_pid_file(switcher: ClaudeAccountSwitcher) -> Path:
    switcher._setup_directories()
    pid_path = switcher.backup_dir / "auto-switch-monitor.pid"
    pid_path.write_text("12345", encoding="utf-8")
    return pid_path


class TestRunCliMonitorServiceMode:
    def test_pid_collision_exits_75_in_service_mode(self, temp_home: Path, capsys):
        switcher = ClaudeAccountSwitcher()
        _write_pid_file(switcher)

        with patch("claude_swap.monitor._pid_is_running", return_value=True):
            code = monitor.run_cli_monitor(switcher, once=True, service_mode=True)

        assert code == monitor.MONITOR_ALREADY_RUNNING_RETRY_EXIT
        assert "already running (pid 12345)" in capsys.readouterr().out

    def test_pid_collision_exits_0_without_service_mode(
        self, temp_home: Path, capsys, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv(monitor.SERVICE_MONITOR_ENV_KEY, raising=False)
        switcher = ClaudeAccountSwitcher()
        _write_pid_file(switcher)

        with patch("claude_swap.monitor._pid_is_running", return_value=True):
            code = monitor.run_cli_monitor(switcher, once=True)

        assert code == 0
        assert "already running (pid 12345)" in capsys.readouterr().out

    def test_legacy_env_var_still_arms_service_mode(
        self, temp_home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Services installed by older fork versions forward
        # CSWAP_SERVICE_MONITOR=1 instead of the argv flag; they must keep
        # their retry semantics until the user reinstalls.
        monkeypatch.setenv(monitor.SERVICE_MONITOR_ENV_KEY, "1")
        switcher = ClaudeAccountSwitcher()
        _write_pid_file(switcher)

        with patch("claude_swap.monitor._pid_is_running", return_value=True):
            code = monitor.run_cli_monitor(switcher, once=True)

        assert code == monitor.MONITOR_ALREADY_RUNNING_RETRY_EXIT


class TestCliServiceMonitorFlag:
    def test_flag_dispatches_service_mode(self):
        with (
            patch("claude_swap.cli.ClaudeAccountSwitcher") as switcher_cls,
            patch.object(
                sys, "argv", ["claude-swap", "--monitor", "--service-monitor"]
            ),
            patch("claude_swap.monitor.run_cli_monitor", return_value=0) as mock_run,
            patch("os.geteuid", return_value=1000),
            patch("claude_swap.update_check.check_for_update", return_value=None),
        ):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 0
        mock_run.assert_called_once_with(switcher_cls.return_value, service_mode=True)

    def test_flag_alone_is_rejected(self, capsys):
        # Without --monitor the required action group already fails parsing;
        # either way the flag must never be accepted on its own.
        with patch.object(sys, "argv", ["claude-swap", "--service-monitor"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2

    def test_flag_rejected_with_non_monitor_action(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "--list", "--service-monitor"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert (
            "--service-monitor can only be used with --monitor"
            in capsys.readouterr().err
        )

    def test_flag_is_hidden_from_help(self, capsys):
        with patch.object(sys, "argv", ["claude-swap", "--help"]):
            with pytest.raises(SystemExit):
                cli.main()

        assert "--service-monitor" not in capsys.readouterr().out
