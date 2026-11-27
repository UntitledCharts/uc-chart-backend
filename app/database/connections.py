from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.database.core import Connection, fetch_all, fetch_one

Provider = Literal["discord", "google"]


class ConnectionRow(BaseModel):
    """A sign-in provider account linked to an account.

    Attributes:
        sonolus_id: Linked account id.
        provider: Provider name.
        provider_user_id: Account id at the provider.
        provider_display_name: Display name at the provider.
        access_token_encrypted: Encrypted provider access token.
        refresh_token_encrypted: Encrypted provider refresh token.
        token_expires_at: Provider access token expiry.
        linked_at: Link time.
    """

    sonolus_id: str
    provider: Provider
    provider_user_id: str
    provider_display_name: str | None = None
    access_token_encrypted: bytes | None = None
    refresh_token_encrypted: bytes | None = None
    token_expires_at: datetime | None = None
    linked_at: datetime


async def list_connections(connection: Connection, sonolus_id: str) -> list[ConnectionRow]:
    """List an account's linked providers.

    Args:
        connection: Database connection.
        sonolus_id: Account id.

    Returns:
        list[ConnectionRow]: The links.
    """
    return await fetch_all(
        connection,
        ConnectionRow,
        "SELECT * FROM account_connections WHERE sonolus_id = $1 ORDER BY provider",
        sonolus_id,
    )


async def get_connection(
    connection: Connection, sonolus_id: str, provider: Provider
) -> ConnectionRow | None:
    """Fetch one linked provider of an account.

    Args:
        connection: Database connection.
        sonolus_id: Account id.
        provider: Provider name.

    Returns:
        ConnectionRow | None: The link, or None.
    """
    return await fetch_one(
        connection,
        ConnectionRow,
        "SELECT * FROM account_connections WHERE sonolus_id = $1 AND provider = $2",
        sonolus_id,
        provider,
    )


async def find_by_provider_user(
    connection: Connection, provider: Provider, provider_user_id: str
) -> ConnectionRow | None:
    """Find the account a provider account is linked to.

    Args:
        connection: Database connection.
        provider: Provider name.
        provider_user_id: Account id at the provider.

    Returns:
        ConnectionRow | None: The link, or None.
    """
    return await fetch_one(
        connection,
        ConnectionRow,
        "SELECT * FROM account_connections WHERE provider = $1 AND provider_user_id = $2",
        provider,
        provider_user_id,
    )


async def save_connection(
    connection: Connection,
    sonolus_id: str,
    provider: Provider,
    provider_user_id: str,
    display_name: str | None,
    access_token_encrypted: bytes | None,
    refresh_token_encrypted: bytes | None,
    token_expires_at: datetime | None,
) -> None:
    """Link a provider account or refresh the stored link.

    Args:
        connection: Database connection.
        sonolus_id: Account id.
        provider: Provider name.
        provider_user_id: Account id at the provider.
        display_name: Display name at the provider.
        access_token_encrypted: Encrypted access token, None when not stored.
        refresh_token_encrypted: Encrypted refresh token, None when not stored.
        token_expires_at: Access token expiry.
    """
    await connection.execute(
        """
        INSERT INTO account_connections
            (sonolus_id, provider, provider_user_id, provider_display_name,
             access_token_encrypted, refresh_token_encrypted, token_expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (sonolus_id, provider) DO UPDATE SET
            provider_user_id = EXCLUDED.provider_user_id,
            provider_display_name = EXCLUDED.provider_display_name,
            access_token_encrypted = EXCLUDED.access_token_encrypted,
            refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
            token_expires_at = EXCLUDED.token_expires_at
        """,
        sonolus_id,
        provider,
        provider_user_id,
        display_name,
        access_token_encrypted,
        refresh_token_encrypted,
        token_expires_at,
    )


async def delete_connection(connection: Connection, sonolus_id: str, provider: Provider) -> bool:
    """Unlink a provider.

    Args:
        connection: Database connection.
        sonolus_id: Account id.
        provider: Provider name.

    Returns:
        bool: Whether a link was removed.
    """
    deleted = await connection.fetchval(
        "DELETE FROM account_connections WHERE sonolus_id = $1 AND provider = $2 RETURNING true",
        sonolus_id,
        provider,
    )
    return bool(deleted)
