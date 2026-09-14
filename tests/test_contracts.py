from datetime import date

import pytest

from signal_engine.data.contracts import (
    ContractRegistry, parse_contract_code, third_friday, build_contract_code,
)


def test_parse_contract_code():
    ci = parse_contract_code("ESZ25")
    assert ci.symbol == "ES"
    assert ci.month_code == "Z"
    assert ci.year == 2025
    assert ci.expiration == third_friday(2025, 12)


def test_parse_micro_and_full_year():
    assert parse_contract_code("MNQH2026").symbol == "MNQ"
    assert parse_contract_code("MNQH2026").year == 2026


def test_third_friday_known_values():
    # Dec 2025: Fridays 5,12,19,26 -> third = 19
    assert third_friday(2025, 12) == date(2025, 12, 19)
    # Mar 2026: Fridays 6,13,20,27 -> third = 20
    assert third_friday(2026, 3) == date(2026, 3, 20)


def test_build_contract_code_roundtrip():
    assert build_contract_code("ES", "Z", 2025) == "ESZ25"


def test_invalid_code_raises():
    with pytest.raises(ValueError):
        parse_contract_code("NOTACODE")


def test_registry_specs_present():
    reg = ContractRegistry.load()
    for sym in ("ES", "NQ", "MES", "MNQ"):
        spec = reg.spec(sym)
        assert spec.multiplier > 0
        assert spec.tick_size == 0.25
        # tick_value consistency with multiplier * tick_size (provisional values)
        assert spec.tick_value == pytest.approx(spec.multiplier * spec.tick_size)
    assert set(("ES", "NQ", "MES", "MNQ")).issubset(set(reg.symbols()))


def test_registry_unverified_guard():
    """Specs are NOT verified against CME yet -> guard must refuse."""
    reg = ContractRegistry.load()
    assert reg.verified is False
    with pytest.raises(RuntimeError):
        reg.require_verified()
