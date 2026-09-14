"""Contract registry and futures contract-code utilities.

Specs are loaded from the centralized, versioned config (``contracts.yaml``)
and are NEVER hardcoded in strategy/risk logic. The ``verified`` flag reflects
whether the specs were confirmed against official CME documentation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from ..config.schema import ContractSpec, ContractsFile

# Standard futures month codes (full set) -> month number.
MONTH_CODES: dict[str, int] = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}
_CODE_BY_MONTH = {v: k for k, v in MONTH_CODES.items()}

# e.g. ESZ25, MNQH2025, NQU5
_CONTRACT_RE = re.compile(r"^(?P<sym>[A-Z]{1,3})(?P<mon>[FGHJKMNQUVXZ])(?P<yr>\d{1,4})$")


@dataclass(frozen=True)
class ContractInfo:
    symbol: str          # "ES"
    contract_code: str   # "ESZ25"
    month_code: str      # "Z"
    year: int            # 2025
    expiration: date     # third Friday (index futures convention)

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.year, MONTH_CODES[self.month_code])


def third_friday(year: int, month: int) -> date:
    """Third Friday of a month (index futures expiration convention)."""
    d = date(year, month, 1)
    offset = (4 - d.weekday()) % 7          # weekday: Mon=0 .. Fri=4
    first_friday = d + timedelta(days=offset)
    return first_friday + timedelta(days=14)


def _normalize_year(yr: str) -> int:
    if len(yr) >= 4:
        return int(yr)
    if len(yr) == 1:
        # single digit: assume current decade (2020s). Ambiguous -> document.
        return 2020 + int(yr)
    return 2000 + int(yr)


def parse_contract_code(code: str) -> ContractInfo:
    m = _CONTRACT_RE.match(code.strip().upper())
    if not m:
        raise ValueError(f"invalid contract code: {code!r}")
    sym = m.group("sym")
    mon = m.group("mon")
    year = _normalize_year(m.group("yr"))
    exp = third_friday(year, MONTH_CODES[mon])
    return ContractInfo(symbol=sym, contract_code=code.strip().upper(),
                        month_code=mon, year=year, expiration=exp)


def build_contract_code(symbol: str, month_code: str, year: int) -> str:
    return f"{symbol}{month_code}{year % 100:02d}"


class ContractRegistry:
    """Central access point for contract specs. Nothing else should read the
    YAML directly."""

    def __init__(self, file: ContractsFile):
        self._file = file

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "ContractRegistry":
        return cls(ContractsFile.load(path))

    @property
    def verified(self) -> bool:
        return self._file.verified

    @property
    def verification(self) -> dict:
        return dict(self._file.verification)

    def symbols(self) -> list[str]:
        return list(self._file.contracts.keys())

    def spec(self, symbol: str) -> ContractSpec:
        try:
            return self._file.contracts[symbol]
        except KeyError as exc:
            raise KeyError(f"no contract spec for symbol {symbol!r}") from exc

    def require_verified(self) -> None:
        """Guard for downstream consumers (e.g. RiskEngine) that must not
        operate on unverified specs."""
        if not self._file.verified:
            note = self._file.verification.get("note", "")
            raise RuntimeError(
                "ContractSpec values are NOT verified against CME "
                f"(see docs/CME_VERIFICATION.md). {note}"
            )
