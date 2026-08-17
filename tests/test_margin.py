import json
from pathlib import Path

import pytest

from perpdesk.margin import (
    build_tier_index,
    derive_tiers,
    maintenance_margin,
    resolve_margin_table,
)


FIXTURES = Path(__file__).parent / "fixtures"


def test_tier_deduction_makes_schedule_continuous() -> None:
    tiers = derive_tiers(
        [
            {"lowerBound": "0", "maxLeverage": 40},
            {"lowerBound": "150000000", "maxLeverage": 20},
        ]
    )
    epsilon = 0.01
    left = maintenance_margin(150_000_000 - epsilon, tiers)
    right = maintenance_margin(150_000_000 + epsilon, tiers)
    assert left == pytest.approx(right, abs=0.001)
    assert tiers[1].deduction == pytest.approx(1_875_000.0)


def test_missing_margin_table_uses_bare_id_as_leverage() -> None:
    raw = resolve_margin_table(5, {})
    assert raw == [{"lowerBound": "0.0", "maxLeverage": 5}]


def test_fixture_resolves_every_universe_coin() -> None:
    meta = json.loads((FIXTURES / "meta_20260815.json").read_text())
    index = build_tier_index(meta)
    assert set(index) == {entry["name"] for entry in meta["universe"]}
    for entry in meta["universe"]:
        assert index[entry["name"]][0].max_leverage == int(entry["maxLeverage"])


@pytest.mark.parametrize("bad_id", [0, -1])
def test_invalid_missing_table_id_fails_loudly(bad_id: int) -> None:
    with pytest.raises(ValueError):
        resolve_margin_table(bad_id, {})
