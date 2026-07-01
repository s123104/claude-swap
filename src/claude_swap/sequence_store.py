"""Typed model and store for ``sequence.json``.

``sequence.json`` is claude-swap's registry of managed accounts. Historically it
was passed around ``switcher.py`` as a raw ``dict`` and indexed by string keys in
dozens of places, which is why ``switcher`` could not be type-checked strictly.

This module owns the on-disk shape behind a typed facade:

* :class:`AccountRecord` and :class:`SequenceData` are frozen, raw-backed views —
  they expose typed accessors while round-tripping the *exact* on-disk JSON
  (unknown/future keys and key presence are preserved verbatim, so the org-field
  migration's ``"organizationUuid" not in acc`` presence check keeps working and
  no upstream field is ever dropped).
* Mutations return new instances (immutable), matching the project's
  immutability rule.
* :class:`SequenceStore` handles load/save only. It does **not** take the file
  lock — callers own ``FileLock`` around whole read-modify-write transactions, so
  the store must stay lock-agnostic to preserve that topology.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from claude_swap.models import get_timestamp

# SSOT for the auto-switch default. Re-exported by ``switcher`` for back-compat.
DEFAULT_AUTO_SWITCH_THRESHOLD = 95

ReadJson = Callable[[Path], "dict[str, Any] | None"]
WriteJson = Callable[[Path, "dict[str, Any]"], None]


@dataclass(frozen=True)
class AutoSwitchConfig:
    """Persisted auto-switch (Beta) settings from the ``autoSwitch`` key."""

    enabled: bool
    threshold: int

    @classmethod
    def from_raw(cls, raw: dict[str, Any] | None) -> AutoSwitchConfig:
        cfg = raw or {}
        try:
            threshold = int(cfg.get("threshold", DEFAULT_AUTO_SWITCH_THRESHOLD))
        except (TypeError, ValueError):
            threshold = DEFAULT_AUTO_SWITCH_THRESHOLD
        return cls(
            enabled=bool(cfg.get("enabled", False)),
            threshold=threshold,
        )


@dataclass(frozen=True)
class AccountRecord:
    """A single managed-account slot, backed by its exact on-disk dict."""

    raw: dict[str, Any]

    @property
    def email(self) -> str:
        return str(self.raw.get("email", ""))

    @property
    def organization_uuid(self) -> str:
        return str(self.raw.get("organizationUuid", "") or "")

    @property
    def organization_name(self) -> str:
        return str(self.raw.get("organizationName", "") or "")

    @property
    def kind(self) -> str:
        """``"api_key"`` or ``"oauth"`` (default for kindless legacy slots)."""
        return "api_key" if self.raw.get("kind") == "api_key" else "oauth"

    @classmethod
    def create(
        cls,
        *,
        email: str,
        uuid: str = "",
        organization_uuid: str = "",
        organization_name: str = "",
        added: str | None = None,
        is_api_key: bool = False,
    ) -> AccountRecord:
        """Build a record in the canonical on-disk key order."""
        raw: dict[str, Any] = {
            "email": email,
            "uuid": uuid,
            "organizationUuid": organization_uuid,
            "organizationName": organization_name,
            "added": added if added is not None else get_timestamp(),
        }
        if is_api_key:
            raw["kind"] = "api_key"
        return cls(raw)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)


@dataclass(frozen=True)
class SequenceData:
    """The whole ``sequence.json`` document, backed by its exact on-disk dict."""

    raw: dict[str, Any]

    @property
    def active_account_number(self) -> int | None:
        value = self.raw.get("activeAccountNumber")
        return int(value) if value is not None else None

    @property
    def last_updated(self) -> str:
        return str(self.raw.get("lastUpdated", ""))

    @property
    def sequence(self) -> tuple[int, ...]:
        return tuple(int(n) for n in self.raw.get("sequence", []))

    @property
    def accounts(self) -> dict[str, AccountRecord]:
        raw_accounts = self.raw.get("accounts", {})
        return {
            str(num): AccountRecord(record)
            for num, record in raw_accounts.items()
        }

    @property
    def auto_switch(self) -> AutoSwitchConfig:
        return AutoSwitchConfig.from_raw(self.raw.get("autoSwitch"))

    def get(self, account_num: str) -> AccountRecord | None:
        record = self.raw.get("accounts", {}).get(str(account_num))
        return AccountRecord(record) if record is not None else None

    @classmethod
    def empty(cls) -> SequenceData:
        """A fresh document matching ``_init_sequence_file``'s shape."""
        return cls(
            {
                "activeAccountNumber": None,
                "lastUpdated": get_timestamp(),
                "sequence": [],
                "accounts": {},
            }
        )

    def _copy(self) -> dict[str, Any]:
        data = copy.deepcopy(self.raw)
        data.setdefault("accounts", {})
        data.setdefault("sequence", [])
        return data

    def register_slot(
        self, account_num: str, record: AccountRecord, *, set_active: bool
    ) -> SequenceData:
        """Add/replace a slot and keep ``sequence`` sorted (immutable)."""
        data = self._copy()
        data["accounts"][str(account_num)] = record.to_dict()
        num = int(account_num)
        if num not in data["sequence"]:
            data["sequence"].append(num)
            data["sequence"].sort()
        if set_active:
            data["activeAccountNumber"] = num
        return SequenceData(data)

    def remove_slot(self, account_num: str) -> SequenceData:
        """Drop a slot from ``accounts`` and ``sequence`` (immutable).

        Filters *every* matching entry from ``sequence`` (not just the first)
        so a corrupt array with duplicate slot ids can't leave a straggler —
        matching the pre-extraction ``remove_account`` behaviour.
        """
        data = self._copy()
        num = int(account_num)
        data["sequence"] = [n for n in data["sequence"] if n != num]
        data["accounts"].pop(str(account_num), None)
        return SequenceData(data)

    def set_active(self, account_num: int | None) -> SequenceData:
        data = self._copy()
        data["activeAccountNumber"] = account_num
        return SequenceData(data)

    def with_auto_switch(
        self, *, enabled: bool | None = None, threshold: int | None = None
    ) -> SequenceData:
        """Merge auto-switch fields, keeping unspecified ones (immutable)."""
        data = self._copy()
        cfg = dict(data.get("autoSwitch") or {})
        if enabled is not None:
            cfg["enabled"] = bool(enabled)
        if threshold is not None:
            cfg["threshold"] = int(threshold)
        cfg.setdefault("enabled", False)
        cfg.setdefault("threshold", DEFAULT_AUTO_SWITCH_THRESHOLD)
        data["autoSwitch"] = cfg
        return SequenceData(data)

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.raw)


class SequenceStore:
    """Load/save ``sequence.json`` through the switcher's atomic JSON helpers.

    Lock-agnostic by design: callers wrap read-modify-write transactions in
    ``FileLock`` themselves, so ``save`` must never take the lock (``FileLock``
    is not re-entrant).
    """

    def __init__(
        self,
        sequence_file: Path,
        *,
        read_json: ReadJson,
        write_json: WriteJson,
    ) -> None:
        self._sequence_file = sequence_file
        self._read_json = read_json
        self._write_json = write_json

    def load_raw(self) -> dict[str, Any] | None:
        """Raw dict as stored, or ``None`` when absent/corrupt.

        Kept for the dict-returning accessors that external modules
        (monitor/list_reporter/migrations) still consume via protocols.
        """
        return self._read_json(self._sequence_file)

    def load(self) -> SequenceData | None:
        raw = self._read_json(self._sequence_file)
        return SequenceData(raw) if raw else None

    def load_or_empty(self) -> SequenceData:
        return self.load() or SequenceData.empty()

    def init_if_missing(self) -> None:
        if not self._sequence_file.exists():
            self._write_json(self._sequence_file, SequenceData.empty().to_dict())

    def save(self, data: SequenceData) -> None:
        """Stamp ``lastUpdated`` and persist. Never takes the file lock."""
        raw = data.to_dict()
        raw["lastUpdated"] = get_timestamp()
        self._write_json(self._sequence_file, raw)
