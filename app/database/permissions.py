from datetime import datetime

from pydantic import BaseModel

from app.database.core import Connection, fetch_all, fetch_value


class PermissionGrantRow(BaseModel):
    """A staff permission held by an account.

    Attributes:
        permission: Permission name.
        granted_by: Account that granted it, if known.
        granted_at: Grant time.
    """

    permission: str
    granted_by: str | None = None
    granted_at: datetime


async def get_permissions(connection: Connection, sonolus_id: str) -> frozenset[str]:
    """Fetch an account's staff permissions.

    Args:
        connection: Database connection.
        sonolus_id: Account id.

    Returns:
        frozenset[str]: Permission names.
    """
    records = await connection.fetch(
        "SELECT permission FROM account_permissions WHERE sonolus_id = $1", sonolus_id
    )
    return frozenset(record["permission"] for record in records)


async def get_permissions_for(
    connection: Connection, sonolus_ids: list[str]
) -> dict[str, frozenset[str]]:
    """Fetch the staff permissions of several accounts.

    Args:
        connection: Database connection.
        sonolus_ids: Account ids.

    Returns:
        dict[str, frozenset[str]]: Permission names keyed by account id; accounts without any are left out.
    """
    if not sonolus_ids:
        return {}
    records = await connection.fetch(
        "SELECT sonolus_id, permission FROM account_permissions WHERE sonolus_id = ANY($1::text[])",
        list(set(sonolus_ids)),
    )
    grouped: dict[str, set[str]] = {}
    for record in records:
        grouped.setdefault(record["sonolus_id"], set()).add(record["permission"])
    return {key: frozenset(value) for key, value in grouped.items()}


async def list_grants(connection: Connection, sonolus_id: str) -> list[PermissionGrantRow]:
    """List an account's permission grants.

    Args:
        connection: Database connection.
        sonolus_id: Account id.

    Returns:
        list[PermissionGrantRow]: The grants, sorted by name.
    """
    return await fetch_all(
        connection,
        PermissionGrantRow,
        """
        SELECT permission, granted_by, granted_at FROM account_permissions
        WHERE sonolus_id = $1 ORDER BY permission
        """,
        sonolus_id,
    )


async def grant_permission(
    connection: Connection, sonolus_id: str, permission: str, granted_by: str | None
) -> bool:
    """Grant a staff permission.

    Args:
        connection: Database connection.
        sonolus_id: Account id.
        permission: Permission name.
        granted_by: Granting account, or None for the admin key.

    Returns:
        bool: Whether the permission was newly granted.
    """
    inserted = await fetch_value(
        connection,
        """
        INSERT INTO account_permissions (sonolus_id, permission, granted_by)
        VALUES ($1, $2, $3)
        ON CONFLICT DO NOTHING
        RETURNING true
        """,
        sonolus_id,
        permission,
        granted_by,
    )
    return bool(inserted)


async def revoke_permission(connection: Connection, sonolus_id: str, permission: str) -> bool:
    """Revoke a staff permission.

    Args:
        connection: Database connection.
        sonolus_id: Account id.
        permission: Permission name.

    Returns:
        bool: Whether the account held the permission.
    """
    deleted = await fetch_value(
        connection,
        "DELETE FROM account_permissions WHERE sonolus_id = $1 AND permission = $2 RETURNING true",
        sonolus_id,
        permission,
    )
    return bool(deleted)
