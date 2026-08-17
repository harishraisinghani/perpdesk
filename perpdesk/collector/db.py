"""Lakebase connection pool with a fresh OAuth credential per connection."""

from __future__ import annotations

from collections.abc import Callable

import psycopg
from databricks.sdk import WorkspaceClient
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import Settings


class OAuthConnection(psycopg.Connection):
    credential_provider: Callable[[], str] | None = None

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs):  # type: ignore[override]
        if cls.credential_provider is None:
            raise RuntimeError("OAuth credential provider was not configured")
        kwargs["password"] = cls.credential_provider()
        return super().connect(conninfo, **kwargs)


def create_pool(
    settings: Settings,
    *,
    open_pool: bool = True,
    min_size: int = 1,
    max_size: int | None = None,
    max_lifetime: float = 3300,
    check: Callable[[psycopg.Connection], None] | None = None,
) -> ConnectionPool:
    """Build the Lakebase pool. The defaults suit a long-lived collector
    process; serverless callers pass a smaller, self-checking profile."""
    settings.validate_database()
    workspace = WorkspaceClient()

    def credential() -> str:
        result = workspace.postgres.generate_database_credential(endpoint=settings.endpoint_name)
        if not result.token:
            raise RuntimeError("Databricks returned an empty database credential")
        return result.token

    OAuthConnection.credential_provider = credential
    conninfo = (
        f"host={settings.pg_host} port={settings.pg_port} dbname={settings.pg_database} "
        f"user={settings.pg_user} sslmode=require"
    )
    return ConnectionPool(
        conninfo,
        connection_class=OAuthConnection,
        kwargs={"row_factory": dict_row},
        min_size=min_size,
        max_size=max(4, settings.rest_concurrency) if max_size is None else max_size,
        max_lifetime=max_lifetime,
        check=check,
        open=open_pool,
        name="perpdesk",
    )
