"""Golden and contract tests for usage_policy SSOT.

Characterization tests pin the observable decisions of the headroom ranking
behind ``--strategy best``. The engine orders its own candidates, so this
module is the single remaining ranking surface.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import oauth
from claude_swap.usage_policy import rank_headroom_best
from tests.conftest import bootstrap_switchable_accounts


def _usage(pct: float, *, resets_at: str | None = None) -> dict:
    entry: dict = {"pct": pct}
    if resets_at is not None:
        entry["resets_at"] = resets_at
    return {"five_hour": entry, "seven_day": {"pct": 0.0}}


def _headroom_select(
    temp_home: Path,
    current: str,
    usage_map: dict[str, object],
    *,
    num_accounts: int = 3,
) -> tuple[str | None, str]:
    s = bootstrap_switchable_accounts(temp_home, num_accounts=num_accounts)
    with (
        patch.object(s, "_usage_by_account", return_value=usage_map),
        patch.object(s, "_account_is_switchable", return_value=True),
    ):
        return s._select_best_switchable(current)


class TestHeadroomGolden:
    """Pin _select_best_switchable / --strategy best decisions."""

    def test_switches_to_strictly_better_headroom(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": _usage(50), "2": _usage(90), "3": _usage(20)},
        )
        assert target == "3"
        assert note == ""

    def test_stays_when_current_is_best(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": _usage(89), "2": _usage(100)},
            num_accounts=2,
        )
        assert target is None
        assert note == "stay"

    def test_all_exhausted(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": _usage(100), "2": _usage(100), "3": _usage(100)},
        )
        assert target is None
        assert note == "exhausted"

    def test_current_unavailable(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": None, "2": _usage(10)},
        )
        assert target is None
        assert note == "current-unavailable"

    def test_no_comparison(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": _usage(50), "2": None},
        )
        assert target is None
        assert note == "no-comparison"

    def test_incomplete_comparison(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": _usage(50), "2": _usage(90), "3": None},
        )
        assert target is None
        assert note == "incomplete-comparison"

    def test_tie_resolves_to_stay(self, temp_home: Path):
        target, note = _headroom_select(
            temp_home,
            "1",
            {"1": _usage(50), "2": _usage(50)},
            num_accounts=2,
        )
        assert target is None
        assert note == "stay"


class TestScoringContract:
    """Direct contract of the ranking function and the shared parsers."""

    @pytest.mark.parametrize(
        "usages,current,others,expected",
        [
            pytest.param(
                {"1": _usage(50), "2": _usage(90), "3": _usage(20)},
                "1",
                ["2", "3"],
                ("3", ""),
                id="picks-lowest-utilization",
            ),
            pytest.param(
                {"1": _usage(100), "2": _usage(100), "3": _usage(100)},
                "1",
                ["2", "3"],
                (None, "exhausted"),
                id="all-saturated-is-exhausted",
            ),
            pytest.param(
                {"1": _usage(100), "2": _usage(100), "3": _usage(30)},
                "1",
                ["2", "3"],
                ("3", ""),
                id="prefers-unsaturated",
            ),
        ],
    )
    def test_rank_headroom_best(
        self,
        usages: dict[str, object],
        current: str,
        others: list[str],
        expected: tuple[str | None, str],
    ):
        result = rank_headroom_best(usages, current=current, others=others)
        assert (result.target, result.note or "") == expected

    @pytest.mark.parametrize(
        "usage,expected_headroom,expected_binding",
        [
            ({"five_hour": {"pct": 80.0}, "seven_day": {"pct": 20.0}}, 20.0, 80.0),
            ({"five_hour": {"pct": 40.0}}, 60.0, 40.0),
            ({"spend": {"pct": 99.0}, "five_hour": {"pct": 10.0}}, 90.0, 10.0),
            ({}, None, None),
            (None, None, None),
        ],
    )
    def test_parser_ssot(
        self,
        usage: object,
        expected_headroom: float | None,
        expected_binding: float | None,
    ):
        from claude_swap.usage_policy import binding_pct, headroom

        assert headroom(usage) == expected_headroom
        assert binding_pct(usage) == expected_binding
        assert oauth.account_headroom(usage) == expected_headroom
