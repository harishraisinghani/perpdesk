import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app.data import _joint_closer_by_pp, _market_action, _pool_profile, _top_market_coins
from app.main import _normalize_candles, app, repository


@pytest.fixture(autouse=True)
def _writable_by_default(monkeypatch) -> None:
    """Keep the ambient environment out of the tests; individual cases opt in to
    read-only or serverless by setting these back."""
    monkeypatch.delenv("PERPDESK_READ_ONLY", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("PERPDESK_SERVERLESS", "false")


def client() -> TestClient:
    os.environ["PERPDESK_DEMO_MODE"] = "true"
    repository.cache_clear()
    return TestClient(app)


def test_dashboard_is_coverage_aware() -> None:
    response = client().get("/api/dashboard?coin=BTC")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "demo"
    assert 0 < body["coverage_fraction_open_interest"] < 1
    assert body["scenarios"][-1]["liquidatable_notional_tracked"] >= body["scenarios"][0]["liquidatable_notional_tracked"]
    assert body["upside_5"]["shock_pct"] == 5
    assert "joint_gap" not in body
    assert "lower bounds" in body["limitations"][0]


def test_market_scanner_is_ranked_by_symmetric_five_percent_risk() -> None:
    body = client().get("/api/dashboard?coin=BTC").json()
    rows = body["markets"]

    assert rows
    assert [row["risk_share_of_tracked"] for row in rows] == sorted(
        [row["risk_share_of_tracked"] for row in rows], reverse=True
    )
    assert {row["coin"] for row in rows}.issubset(set(body["coins"]))
    assert all(row["dominant"] in {"downside", "upside", "balanced"} for row in rows)
    assert all(row["action"] in {"long", "short", "wait"} for row in rows)
    assert all("trend_fraction" in row and row["action_reason"] for row in rows)


def test_market_action_requires_trend_confirmation() -> None:
    assert _market_action("upside", 0.012)[0] == "long"
    assert _market_action("downside", -0.012)[0] == "short"
    assert _market_action("upside", -0.012)[0] == "wait"
    assert _market_action("downside", 0.003)[0] == "wait"
    assert _market_action("balanced", 0.02)[0] == "wait"
    assert _market_action("upside", None)[0] == "wait"


def test_dashboard_html_uses_trader_facing_metrics() -> None:
    html = client().get("/").text

    assert "Joint vs. marginal" not in html
    assert "5% upside" in html
    assert "Market risk scanner" in html
    assert "1D spot trend" in html
    assert "Action" in html
    assert "Tracked long notional that would be liquidatable" in html
    assert "asset price" in html
    assert "price-chart" in html


def test_dashboard_timestamps_the_mark_separately_from_positions() -> None:
    body = client().get("/api/dashboard?coin=BTC").json()

    assert body["mark_as_of"].endswith("Z")
    assert body["mark_as_of"] >= body["as_of"]


def test_candle_normalization_is_sorted_deduplicated_and_limited() -> None:
    rows = [
        {"t": 2_000, "o": "11", "h": "13", "l": "10", "c": "12", "v": "5"},
        {"t": 1_000, "o": "10", "h": "12", "l": "9", "c": "11", "v": "4"},
        {"t": 2_000, "o": "11", "h": "14", "l": "10", "c": "13", "v": "6"},
    ]

    assert _normalize_candles(rows, 2) == [
        {"time": 1, "open": 10.0, "high": 12.0, "low": 9.0, "close": 11.0, "volume": 4.0},
        {"time": 2, "open": 11.0, "high": 14.0, "low": 10.0, "close": 13.0, "volume": 6.0},
    ]


def test_candle_endpoint_proxies_hyperliquid(monkeypatch) -> None:
    rows = [
        {"t": 1_000, "o": "10", "h": "12", "l": "9", "c": "11", "v": "4"},
    ]

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return rows

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, json):
            assert json["type"] == "candleSnapshot"
            assert json["req"]["coin"] == "BTC"
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    response = client().get("/api/candles?coin=BTC&interval=15m&limit=24")

    assert response.status_code == 200
    assert response.json()["candles"][0]["close"] == 11.0


def test_cliffs_are_actionable_and_ordered_nearest_first() -> None:
    body = client().get("/api/dashboard?coin=BTC").json()
    drops = [row["drop_pct"] for row in body["cliffs"]]

    assert drops == sorted(drops)
    assert all(0 < drop <= 20 for drop in drops)


def test_market_list_is_top_ten_by_open_interest_notional() -> None:
    marks = {
        f"COIN{index}": {"open_interest_notional": float(index)}
        for index in range(12)
    }

    assert _top_market_coins(marks) == [
        "COIN11",
        "COIN10",
        "COIN9",
        "COIN8",
        "COIN7",
        "COIN6",
        "COIN5",
        "COIN4",
        "COIN3",
        "COIN2",
    ]


def test_joint_gap_has_the_same_meaning_for_longs_and_shorts() -> None:
    assert round(_joint_closer_by_pp(0.95, 0.90), 8) == 5.0
    assert round(_joint_closer_by_pp(1.05, 1.10), 8) == 5.0
    assert round(_joint_closer_by_pp(0.85, 0.90), 8) == -5.0
    assert _joint_closer_by_pp(0.95, None) is None


def test_promotion_validates_public_address() -> None:
    response = client().post("/api/watchlist/promote", json={"address": "not-an-address"})
    assert response.status_code == 422
    accepted = client().post(
        "/api/watchlist/promote",
        json={"address": "0x" + "a" * 40},
    )
    assert accepted.status_code == 202


def test_read_only_rejects_writes_but_still_serves_reads(monkeypatch) -> None:
    monkeypatch.setenv("PERPDESK_READ_ONLY", "true")
    published = client()

    assert published.get("/api/dashboard?coin=BTC").status_code == 200
    assert published.get("/api/alerts").status_code == 200
    # A valid address must still be refused: the guard has to run before
    # validation, or a public visitor learns which addresses would be accepted.
    assert published.post(
        "/api/watchlist/promote", json={"address": "0x" + "a" * 40}
    ).status_code == 403
    assert published.patch("/api/alerts/1/acknowledge").status_code == 403


def test_read_only_is_advertised_so_the_ui_can_hide_write_controls(monkeypatch) -> None:
    assert client().get("/api/dashboard?coin=BTC").json()["read_only"] is False

    monkeypatch.setenv("PERPDESK_READ_ONLY", "true")
    published = client()
    assert published.get("/api/dashboard?coin=BTC").json()["read_only"] is True
    assert published.get("/api/alerts").json()["read_only"] is True
    assert published.get("/health").json()["read_only"] is True


def test_serverless_defaults_to_read_only_without_being_asked(monkeypatch) -> None:
    monkeypatch.delenv("PERPDESK_SERVERLESS", raising=False)
    monkeypatch.setenv("VERCEL", "1")

    assert client().get("/health").json()["read_only"] is True


def test_serverless_pool_profile_is_small_and_liveness_checked() -> None:
    assert _pool_profile() == {}

    os.environ["PERPDESK_SERVERLESS"] = "true"
    try:
        profile = _pool_profile()
    finally:
        os.environ["PERPDESK_SERVERLESS"] = "false"

    assert profile["min_size"] == 0
    assert profile["max_size"] == 2
    # Must be well under the frozen-instance lifetime and the collector's 3300s.
    assert profile["max_lifetime"] <= 600
    assert profile["check"] is not None
