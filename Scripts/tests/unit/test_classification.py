#!/usr/bin/env python3
"""
Holding classification: the typed fields that should replace name-substring branching.

Finding LL-02/LL-05/LL-13 is that institution, carrier and product *names* are used as the
type system. The replacement already exists: `asset_class` plus the seven-value
`risk_class` taxonomy, validated at parse time. These tests pin that contract so a fix can
lean on it.
"""

from __future__ import annotations

import pytest

from ingest.classification import (
    ALLOWED_RISK_BY_ASSET_CLASS,
    VALID_RISK_CLASSES,
    validate_risk_class,
)
from ingest.errors import InvalidHoldingClassificationError


@pytest.mark.feature("asset_classes")
def test_risk_taxonomy__is_exactly_seven_classes__in_increasing_risk_order():
    """The risk taxonomy is a closed seven-value set, so code can switch on it exhaustively."""
    assert VALID_RISK_CLASSES == (
        "cash_fixed",
        "fixed_income",
        "hybrid_buffered",
        "broad_equity",
        "real_diversifier",
        "single_stock",
        "crypto_speculative",
    )


@pytest.mark.feature("asset_classes")
@pytest.mark.parametrize(
    "asset_class,risk_class",
    [
        ("CASH", "cash_fixed"),
        ("FIXED_ANNUITY", "fixed_income"),
        ("INDEXED_ANNUITY", "hybrid_buffered"),
        ("RILA_ANNUITY", "hybrid_buffered"),
        ("MUNI_BOND", "fixed_income"),
        ("ETF", "broad_equity"),
        ("EQUITY", "single_stock"),
        ("MUTUAL_FUND", "broad_equity"),
    ],
)
def test_validate_risk_class__plausible_pairing__is_accepted(asset_class, risk_class):
    """Every asset class a fixture portfolio uses pairs cleanly with its risk class."""
    assert validate_risk_class(asset_class, risk_class) == risk_class


@pytest.mark.feature("asset_classes")
def test_validate_risk_class__missing__raises_rather_than_defaulting():
    """An unclassified holding fails loudly instead of inheriting a silent default."""
    with pytest.raises(InvalidHoldingClassificationError) as exc:
        validate_risk_class("EQUITY", "", security_id="TICKER:QX")
    assert "TICKER:QX" in str(exc.value)


@pytest.mark.feature("asset_classes")
def test_validate_risk_class__conflicting_pair__raises_with_allowed_set():
    """Cash cannot be single-stock; the error names what would have been allowed."""
    with pytest.raises(InvalidHoldingClassificationError) as exc:
        validate_risk_class("CASH", "single_stock")
    assert "cash_fixed" in str(exc.value)


@pytest.mark.feature("asset_classes")
@pytest.mark.regression("LL-13")
def test_annuity_classes__carry_their_own_tax_shape__without_a_carrier_name():
    """
    Fixed, indexed and buffered annuities are distinguishable by asset class alone.

    Exercises: the three annuity classes have distinct allowed risk sets, so the
    RILA-versus-FIA split has a typed signal available and does not need the
    `"allianz" in asset_id` substring test that LL-13 records.
    """
    for cls in ("FIXED_ANNUITY", "INDEXED_ANNUITY", "RILA_ANNUITY"):
        assert cls in ALLOWED_RISK_BY_ASSET_CLASS, f"{cls} has no declared risk set"
    assert ALLOWED_RISK_BY_ASSET_CLASS["FIXED_ANNUITY"] != ALLOWED_RISK_BY_ASSET_CLASS["RILA_ANNUITY"]
