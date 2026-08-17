import pytest

from perpdesk.margin import derive_tiers
from perpdesk.shocks import (
    AccountPosition,
    account_value,
    joint_liquidation_multiplier,
    liquidation_state,
    scenario_liquidation_state,
)


UNTIERED = derive_tiers([{"lowerBound": "0", "maxLeverage": 20}])
TIERED = derive_tiers(
    [
        {"lowerBound": "0", "maxLeverage": 40},
        {"lowerBound": "150000000", "maxLeverage": 20},
    ]
)


def test_account_value_identity_uses_signed_positions() -> None:
    positions = [
        AccountPosition("BTC", 2, 100, UNTIERED),
        AccountPosition("ETH", -5, 10, UNTIERED),
    ]
    assert account_value(1_000, positions) == 1_150


def test_closed_form_long_root_agrees_with_brute_force() -> None:
    positions = [
        AccountPosition("BTC", 1, 100, UNTIERED),
        AccountPosition("ETH", -2, 50, UNTIERED),
    ]
    # Current AV = 30 + 100 - 100 = 30; MM = 5. The BTC long fails
    # as BTC falls, while the ETH leg and its maintenance stay fixed.
    root = joint_liquidation_multiplier(30, positions, "BTC")
    assert root is not None
    assert liquidation_state(30, positions, "BTC", root).equity_buffer == pytest.approx(0, abs=1e-10)
    assert not liquidation_state(30, positions, "BTC", root + 1e-6).liquidatable
    assert liquidation_state(30, positions, "BTC", root - 1e-6).liquidatable


def test_closed_form_short_root_agrees_with_brute_force() -> None:
    positions = [AccountPosition("BTC", -1, 100, UNTIERED)]
    root = joint_liquidation_multiplier(125, positions, "BTC", max_multiplier=3)
    assert root is not None
    assert liquidation_state(125, positions, "BTC", root).equity_buffer == pytest.approx(0, abs=1e-10)
    assert not liquidation_state(125, positions, "BTC", root - 1e-6).liquidatable
    assert liquidation_state(125, positions, "BTC", root + 1e-6).liquidatable


def test_solver_handles_tier_crossing() -> None:
    positions = [AccountPosition("BTC", 2_000_000, 100, TIERED)]
    root = joint_liquidation_multiplier(-40_000_000, positions, "BTC")
    assert root is not None
    assert root < 0.75  # root lies below the $150m tier boundary
    assert liquidation_state(-40_000_000, positions, "BTC", root).equity_buffer == pytest.approx(0, abs=1e-6)


def test_grid_and_closed_form_classification_agree() -> None:
    positions = [
        AccountPosition("BTC", 1.2, 60_000, UNTIERED),
        AccountPosition("ETH", 10, 3_000, UNTIERED),
    ]
    total_raw = -80_000
    root = joint_liquidation_multiplier(total_raw, positions, "BTC")
    assert root is not None
    for multiplier in (0.99, 0.95, 0.90, 0.80, 0.70):
        brute = liquidation_state(total_raw, positions, "BTC", multiplier).liquidatable
        closed_form = multiplier < root
        assert brute == closed_form


def test_correlated_scenario_moves_every_leg() -> None:
    positions = [
        AccountPosition("BTC", 1, 100, UNTIERED),
        AccountPosition("ETH", 2, 50, UNTIERED),
    ]
    state = scenario_liquidation_state(0, positions, {"BTC": 0.9, "ETH": 0.8})
    assert state.account_value == 170
    assert state.maintenance_margin == pytest.approx(4.25)


def test_isolated_positions_are_not_blended_into_cross_state() -> None:
    positions = [
        AccountPosition("BTC", 1, 100, UNTIERED),
        AccountPosition("ETH", -100, 50, UNTIERED, leverage_type="isolated"),
    ]
    state = scenario_liquidation_state(10, positions, {"BTC": 0.5, "ETH": 5})
    assert state.account_value == 60
    assert state.maintenance_margin == pytest.approx(1.25)
