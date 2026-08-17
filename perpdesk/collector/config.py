"""Collector configuration loaded once at process start."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    info_url: str
    ws_url: str
    endpoint_name: str
    pg_host: str
    pg_database: str
    pg_user: str
    pg_port: int
    tracked_wallets: int
    t0_size: int
    t1_size: int
    weight_budget: int
    periodic_snapshot_sec: int
    rest_concurrency: int
    cycle_sec: int
    new_account_share: float

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            info_url=os.getenv("HL_INFO_URL", "https://api.hyperliquid.xyz/info"),
            ws_url=os.getenv("HL_WS_URL", "wss://api.hyperliquid.xyz/ws"),
            endpoint_name=os.getenv("ENDPOINT_NAME", ""),
            pg_host=os.getenv("PGHOST", ""),
            pg_database=os.getenv("PGDATABASE", "databricks_postgres"),
            pg_user=os.getenv("PGUSER", ""),
            pg_port=int(os.getenv("PGPORT", "5432")),
            tracked_wallets=int(os.getenv("PERPDESK_TRACKED_WALLETS", "1000")),
            t0_size=int(os.getenv("PERPDESK_T0_SIZE", "10")),
            t1_size=int(os.getenv("PERPDESK_T1_SIZE", "990")),
            weight_budget=int(os.getenv("PERPDESK_WEIGHT_BUDGET", "1180")),
            periodic_snapshot_sec=int(
                os.getenv("PERPDESK_PERIODIC_SNAPSHOT_SEC", "900")
            ),
            rest_concurrency=int(os.getenv("PERPDESK_REST_CONCURRENCY", "6")),
            cycle_sec=int(os.getenv("PERPDESK_CYCLE_SEC", "60")),
            new_account_share=float(
                os.getenv("PERPDESK_NEW_ACCOUNT_SHARE", "1.0")
            ),
        )

    def validate_database(self) -> None:
        missing = [
            name
            for name, value in (
                ("ENDPOINT_NAME", self.endpoint_name),
                ("PGHOST", self.pg_host),
                ("PGUSER", self.pg_user),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f"missing required database settings: {', '.join(missing)}")
        if self.tracked_wallets < 1:
            raise RuntimeError("PERPDESK_TRACKED_WALLETS must be positive")
        if not 0 <= self.t0_size <= self.tracked_wallets:
            raise RuntimeError(
                "PERPDESK_T0_SIZE must be between 0 and PERPDESK_TRACKED_WALLETS"
            )
        if not 0 <= self.new_account_share <= 1:
            raise RuntimeError("PERPDESK_NEW_ACCOUNT_SHARE must be between 0 and 1")
