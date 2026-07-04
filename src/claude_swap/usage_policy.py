"""Usage-window parsing and switch-target ranking for claude-swap.

Single source of truth for reading the 5h/7d rate-limit windows out of a
usage entry (``binding_pct`` / ``headroom``) and for the stay-biased
``--strategy best`` ranking. The auto-switch engine (``autoswitch``) does its
own candidate ordering but shares the window parsing here. No I/O of its own
and no ``switcher`` import; callers inject the usage entries they own.
"""

from __future__ import annotations

from dataclasses import dataclass

# The two windows that actually gate requests. ``spend`` (pay-as-you-go
# extra-usage credits) is a separate axis and deliberately ignored here.
_RATE_LIMIT_KEYS = ("five_hour", "seven_day")


def usage_pcts(usage: object) -> list[float]:
    """Extract valid 5h/7d utilization percentages from a usage entry."""
    if not isinstance(usage, dict):
        return []
    pcts: list[float] = []
    for key in _RATE_LIMIT_KEYS:
        entry = usage.get(key)
        if isinstance(entry, dict):
            pct = entry.get("pct")
            if isinstance(pct, (int, float)):
                pcts.append(float(pct))
    return pcts


def binding_pct(usage: object) -> float | None:
    """Utilization of the binding window — the higher of 5h/7d — or ``None``.

    Whichever window is closer to its limit gates the next request, so it is
    the only percentage worth comparing across slots.
    """
    pcts = usage_pcts(usage)
    return max(pcts) if pcts else None


def headroom(usage: object) -> float | None:
    """Remaining percentage before the binding rate-limit window hits 100%.

    ``<= 0`` means the account is at or over a limit; ``None`` means usage
    is unavailable — unknown, not zero.
    """
    bp = binding_pct(usage)
    if bp is None:
        return None
    return 100.0 - bp


@dataclass(frozen=True)
class RankSlotsResult:
    """A ranked switch target with an optional human-readable decision note."""

    target: str | None
    note: str | None = None


def rank_headroom_best(
    usages: dict[str, object],
    *,
    current: str | None,
    others: list[str],
) -> RankSlotsResult:
    """Stay-biased ``--strategy best`` ranking.

    Compares ``others`` against ``current`` and only names a target it can
    prove strictly better — the ``note`` explains a ``None`` target (see
    ``_select_best_switchable`` in ``switcher`` for the full vocabulary).
    """
    if not others:
        return RankSlotsResult(None, "none")

    current_headroom = headroom(usages.get(str(current)))
    if current_headroom is None:
        return RankSlotsResult(None, "current-unavailable")

    scored = [(headroom(usages.get(num)), num) for num in others]
    known = [(h, num) for h, num in scored if h is not None]
    if not known:
        return RankSlotsResult(None, "no-comparison")

    best_headroom, best_num = max(known, key=lambda t: t[0])
    if best_headroom > current_headroom:
        return RankSlotsResult(best_num, "")

    if any(h is None for h, _ in scored):
        return RankSlotsResult(None, "incomplete-comparison")
    if current_headroom <= 0:
        return RankSlotsResult(None, "exhausted")
    return RankSlotsResult(None, "stay")
