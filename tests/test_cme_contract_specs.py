"""Phase 6.5 — CME contract spec tests.

These tests verify the STRUCTURE, INTERNAL CONSISTENCY, and BLOCKING BEHAVIOR
of the centralized ContractSpec registry for ES/NQ/MES/MNQ. They do NOT and
cannot confirm the values against CME Group (that verification remains
blocked — see docs/CME_VERIFICATION.md); accordingly every test here treats
`verified: false` as the expected, correct state, and asserts that any
consumer requiring verified data is refused.

No internet access is used anywhere in this file.
"""
from __future__ import annotations

import pytest

from signal_engine.config.schema import ContractSpec, ContractsFile
from signal_engine.data.contracts import ContractRegistry

SYMBOLS = ("ES", "NQ", "MES", "MNQ")


@pytest.fixture(scope="module")
def registry() -> ContractRegistry:
    return ContractRegistry.load()


# --------------------------------------------------------------------------- #
# Per-instrument spec presence and field sanity (structure, not CME-truth)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("symbol", SYMBOLS)
def test_spec_present_with_required_fields(registry, symbol):
    spec = registry.spec(symbol)
    assert isinstance(spec, ContractSpec)
    assert spec.symbol == symbol
    assert spec.name
    assert spec.exchange == "CME"
    assert spec.currency == "USD"
    assert spec.multiplier > 0
    assert spec.tick_size > 0
    assert spec.tick_value > 0
    assert spec.months, "contract months list must not be empty"


@pytest.mark.parametrize("symbol,expected_multiplier", [
    ("ES", 50.0), ("NQ", 20.0), ("MES", 5.0), ("MNQ", 2.0),
])
def test_multiplier_matches_centralized_config(registry, symbol, expected_multiplier):
    """These are the PROVISIONAL values currently loaded from contracts.yaml —
    this test guards against silent/accidental edits to the centralized
    config, not against CME's official truth (which remains unverified)."""
    assert registry.spec(symbol).multiplier == expected_multiplier


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_tick_size_is_quarter_point_for_all_index_futures(registry, symbol):
    # All four provisionally share a 0.25-point minimum tick.
    assert registry.spec(symbol).tick_size == 0.25


# --------------------------------------------------------------------------- #
# Tick <-> point relationships (internal arithmetic consistency)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("symbol", SYMBOLS)
def test_tick_value_equals_multiplier_times_tick_size(registry, symbol):
    spec = registry.spec(symbol)
    assert spec.tick_value == pytest.approx(spec.multiplier * spec.tick_size)


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_point_value_alias_matches_multiplier(registry, symbol):
    """`point_value` is a computed alias for `multiplier` (no duplicated
    field/source of truth)."""
    spec = registry.spec(symbol)
    assert spec.point_value == spec.multiplier


@pytest.mark.parametrize("symbol", SYMBOLS)
def test_points_to_dollars_and_ticks_from_points_consistency(registry, symbol):
    spec = registry.spec(symbol)
    ten_points_in_dollars = spec.points_to_dollars(10.0)
    assert ten_points_in_dollars == pytest.approx(10.0 * spec.multiplier)
    ticks_in_ten_points = spec.ticks_from_points(10.0)
    assert ticks_in_ten_points == pytest.approx(10.0 / spec.tick_size)
    # round-trip: N ticks * tick_value == N ticks worth of points * multiplier
    n_ticks = 4.0
    dollars_via_ticks = n_ticks * spec.tick_value
    dollars_via_points = spec.points_to_dollars(n_ticks * spec.tick_size)
    assert dollars_via_ticks == pytest.approx(dollars_via_points)


# --------------------------------------------------------------------------- #
# Micro <-> E-mini relationships (structural, provisional)
# --------------------------------------------------------------------------- #
def test_mes_is_flagged_as_micro_of_es(registry):
    mes = registry.spec("MES")
    assert mes.is_micro is True
    assert mes.micro_of == "ES"


def test_mnq_is_flagged_as_micro_of_nq(registry):
    mnq = registry.spec("MNQ")
    assert mnq.is_micro is True
    assert mnq.micro_of == "NQ"


def test_es_and_nq_are_not_flagged_as_micro(registry):
    assert registry.spec("ES").is_micro is False
    assert registry.spec("ES").micro_of is None
    assert registry.spec("NQ").is_micro is False
    assert registry.spec("NQ").micro_of is None


@pytest.mark.parametrize("micro,full", [("MES", "ES"), ("MNQ", "NQ")])
def test_micro_multiplier_is_one_tenth_of_full_contract(registry, micro, full):
    """Structural consistency of the currently-loaded (provisional) values —
    a Micro contract is documented (pending CME verification) as 1/10th the
    notional of its E-mini counterpart."""
    m = registry.spec(micro)
    f = registry.spec(full)
    assert m.multiplier == pytest.approx(f.multiplier / 10.0)


# --------------------------------------------------------------------------- #
# Quarterly contract cycle (H, M, U, Z)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("symbol", SYMBOLS)
def test_quarterly_month_cycle(registry, symbol):
    assert registry.spec(symbol).months == ["H", "M", "U", "Z"]


# --------------------------------------------------------------------------- #
# verified=False and require_verified() blocking behavior
# --------------------------------------------------------------------------- #
def test_registry_is_not_verified(registry):
    """As of Phase 6.5, CME access remains blocked; this MUST stay False —
    flipping it without real verification would be a correctness bug."""
    assert registry.verified is False


def test_require_verified_blocks_all_current_specs(registry):
    with pytest.raises(RuntimeError, match="NOT verified"):
        registry.require_verified()


def test_require_verified_error_references_documentation(registry):
    with pytest.raises(RuntimeError) as exc_info:
        registry.require_verified()
    assert "CME_VERIFICATION.md" in str(exc_info.value)


def test_verification_metadata_documents_the_block(registry):
    meta = registry.verification
    assert meta.get("source") is None
    assert meta.get("date") is None
    assert "unverified_fields" in meta
    assert len(meta["unverified_fields"]) > 0
    assert "attempts" in meta
    assert len(meta["attempts"]) >= 2      # Phase 3 attempt + Phase 6.5 attempt


def test_require_verified_would_pass_once_flag_is_true():
    """Sanity check on the guard's own logic (not a claim that CME is
    verified): a hypothetically-verified file must NOT raise."""
    verified_file = ContractsFile(
        verified=True,
        verification={"source": "cmegroup.com", "date": "2099-01-01"},
        contracts={"ES": ContractSpec(
            symbol="ES", name="E-mini S&P 500", multiplier=50.0,
            tick_size=0.25, tick_value=12.50, months=["H", "M", "U", "Z"],
        )},
    )
    reg = ContractRegistry(verified_file)
    reg.require_verified()   # must not raise


# --------------------------------------------------------------------------- #
# Centralization: no duplicated values elsewhere in the codebase
# --------------------------------------------------------------------------- #
def test_contract_spec_rejects_non_positive_values():
    with pytest.raises(Exception):
        ContractSpec(symbol="ES", name="x", multiplier=0.0, tick_size=0.25,
                    tick_value=12.5, months=["H"])
    with pytest.raises(Exception):
        ContractSpec(symbol="ES", name="x", multiplier=50.0, tick_size=-0.25,
                    tick_value=12.5, months=["H"])


def test_all_four_instruments_present_exactly_once(registry):
    symbols = registry.symbols()
    assert sorted(symbols) == sorted(SYMBOLS)
    assert len(symbols) == len(set(symbols))
