import pytest

from perpdesk.collector.normalize import (
    normalize_account_state,
    normalize_asset_contexts,
    normalize_meta,
)
from perpdesk.collector.wallets import read_wallet_file


def test_book_hash_ignores_api_position_order() -> None:
    state = {
        "crossMarginSummary": {"totalRawUsd": "10", "accountValue": "20"},
        "crossMaintenanceMarginUsed": "2",
        "assetPositions": [
            {"position": {"coin": "BTC", "szi": "1", "entryPx": "100", "positionValue": "100", "marginUsed": "5", "unrealizedPnl": "0", "leverage": {"type": "cross", "value": 20}}},
            {"position": {"coin": "ETH", "szi": "2", "entryPx": "10", "positionValue": "20", "marginUsed": "1", "unrealizedPnl": "0", "leverage": {"type": "cross", "value": 20}}},
        ],
    }
    account_a, _ = normalize_account_state("0xABC", state)
    state["assetPositions"].reverse()
    account_b, _ = normalize_account_state("0xabc", state)
    assert account_a["book_hash"] == account_b["book_hash"]
    assert account_a["account"] == "0xabc"


def test_meta_normalization_synthesizes_missing_table() -> None:
    meta = {
        "marginTables": [],
        "universe": [{"name": "ATOM", "szDecimals": 2, "maxLeverage": 5, "marginTableId": 5}],
    }
    universe, tiers = normalize_meta(meta)
    assert universe[0]["margin_table_id"] == 5
    assert tiers[0]["max_leverage"] == 5
    assert tiers[0]["synthesised"] is True


def test_newly_discovered_book_defaults_to_tail_tier() -> None:
    state = {
        "crossMarginSummary": {"totalRawUsd": "100", "accountValue": "100"},
        "crossMaintenanceMarginUsed": "0",
        "assetPositions": [],
    }
    account, positions = normalize_account_state("0x" + "a" * 40, state)
    assert account["tier"] == 2
    assert positions == []


def test_asset_context_includes_prior_day_price() -> None:
    payload = [
        {"universe": [{"name": "BTC"}]},
        [{"markPx": "100", "prevDayPx": "98", "openInterest": "5"}],
    ]

    rows = normalize_asset_contexts(payload)

    assert rows[0]["mark_px"] == "100"
    assert rows[0]["prev_day_px"] == "98"


def test_wallet_file_normalizes_and_deduplicates(tmp_path) -> None:
    wallet = "0x" + "a" * 40
    source = tmp_path / "wallets.txt"
    source.write_text(f"{wallet.upper().replace('0X', '0x')}\n{wallet}\n")

    result = read_wallet_file(source)

    assert result.addresses == (wallet,)
    assert result.total_lines == 2
    assert result.duplicate_lines == 1


def test_wallet_file_reports_invalid_line(tmp_path) -> None:
    source = tmp_path / "wallets.txt"
    source.write_text(f"{'0x' + 'a' * 40}\nnot-an-address\n")

    with pytest.raises(ValueError, match=r"wallets\.txt:2"):
        read_wallet_file(source)
