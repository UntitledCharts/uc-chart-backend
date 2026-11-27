from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.database.core import Connection, fetch_all, fetch_one


class CollaboratorRow(BaseModel):
    """A collaborator on a chart.

    Attributes:
        chart_id: Chart id.
        sonolus_id: Collaborator account id.
        sonolus_username: Collaborator display name.
        sonolus_handle: Collaborator handle.
        permissions: Chart permissions granted.
        added_by: Account that added the collaborator.
        added_at: Time they joined.
    """

    chart_id: str
    sonolus_id: str
    sonolus_username: str
    sonolus_handle: int
    permissions: list[str]
    added_by: str | None = None
    added_at: datetime


class InviteRow(BaseModel):
    """A pending collaboration invite.

    Attributes:
        id: Invite id.
        chart_id: Chart id.
        chart_title: Chart title.
        sonolus_id: Invited account id.
        permissions: Chart permissions offered.
        invited_by: Inviting account id.
        created_at: Invite time.
    """

    id: UUID
    chart_id: str
    chart_title: str
    sonolus_id: str
    permissions: list[str]
    invited_by: str | None = None
    created_at: datetime


async def get_collaborator(
    connection: Connection, chart_id: str, sonolus_id: str
) -> CollaboratorRow | None:
    """Fetch one collaborator of a chart.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        sonolus_id: Account id.

    Returns:
        CollaboratorRow | None: The collaborator, or None.
    """
    return await fetch_one(
        connection,
        CollaboratorRow,
        """
        SELECT cc.chart_id, cc.sonolus_id, a.sonolus_username, a.sonolus_handle, cc.permissions, cc.added_by, cc.added_at
        FROM chart_collaborators cc JOIN accounts a ON a.sonolus_id = cc.sonolus_id
        WHERE cc.chart_id = $1 AND cc.sonolus_id = $2
        """,
        chart_id,
        sonolus_id,
    )


async def list_collaborators(connection: Connection, chart_id: str) -> list[CollaboratorRow]:
    """List a chart's collaborators.

    Args:
        connection: Database connection.
        chart_id: Chart id.

    Returns:
        list[CollaboratorRow]: The collaborators, oldest first.
    """
    return await fetch_all(
        connection,
        CollaboratorRow,
        """
        SELECT cc.chart_id, cc.sonolus_id, a.sonolus_username, a.sonolus_handle, cc.permissions, cc.added_by, cc.added_at
        FROM chart_collaborators cc JOIN accounts a ON a.sonolus_id = cc.sonolus_id
        WHERE cc.chart_id = $1
        ORDER BY cc.added_at
        """,
        chart_id,
    )


async def set_permissions(
    connection: Connection, chart_id: str, sonolus_id: str, permissions: list[str]
) -> bool:
    """Replace a collaborator's chart permissions.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        sonolus_id: Collaborator account id.
        permissions: New permissions.

    Returns:
        bool: Whether the collaborator exists.
    """
    updated = await connection.fetchval(
        "UPDATE chart_collaborators SET permissions = $3 WHERE chart_id = $1 AND sonolus_id = $2 RETURNING true",
        chart_id,
        sonolus_id,
        sorted(set(permissions)),
    )
    return bool(updated)


async def remove_collaborator(connection: Connection, chart_id: str, sonolus_id: str) -> bool:
    """Remove a collaborator.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        sonolus_id: Collaborator account id.

    Returns:
        bool: Whether a collaborator was removed.
    """
    deleted = await connection.fetchval(
        "DELETE FROM chart_collaborators WHERE chart_id = $1 AND sonolus_id = $2 RETURNING true",
        chart_id,
        sonolus_id,
    )
    return bool(deleted)


async def create_invite(
    connection: Connection,
    chart_id: str,
    sonolus_id: str,
    permissions: list[str],
    invited_by: str | None,
) -> tuple[UUID, bool]:
    """Invite an account, replacing any pending invite to the same chart.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        sonolus_id: Invited account id.
        permissions: Chart permissions offered.
        invited_by: Inviting account id.

    Returns:
        tuple[UUID, bool]: Invite id, and whether the invite is new rather than a replaced one.
    """
    record = await connection.fetchrow(
        """
        INSERT INTO chart_collaborator_invites (chart_id, sonolus_id, permissions, invited_by)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (chart_id, sonolus_id) DO UPDATE
        SET permissions = EXCLUDED.permissions, invited_by = EXCLUDED.invited_by, created_at = CURRENT_TIMESTAMP
        RETURNING id, (xmax = 0) AS created
        """,
        chart_id,
        sonolus_id,
        sorted(set(permissions)),
        invited_by,
    )
    assert record is not None
    return record["id"], bool(record["created"])


async def list_invites_for_account(connection: Connection, sonolus_id: str) -> list[InviteRow]:
    """List the invites an account received.

    Args:
        connection: Database connection.
        sonolus_id: Invited account id.

    Returns:
        list[InviteRow]: The invites, newest first.
    """
    return await fetch_all(
        connection,
        InviteRow,
        """
        SELECT i.id, i.chart_id, c.title AS chart_title, i.sonolus_id, i.permissions, i.invited_by, i.created_at
        FROM chart_collaborator_invites i JOIN charts c ON c.id = i.chart_id
        WHERE i.sonolus_id = $1 AND c.deleted_at IS NULL
        ORDER BY i.created_at DESC
        """,
        sonolus_id,
    )


async def list_invites_for_chart(connection: Connection, chart_id: str) -> list[InviteRow]:
    """List a chart's pending invites.

    Args:
        connection: Database connection.
        chart_id: Chart id.

    Returns:
        list[InviteRow]: The invites, newest first.
    """
    return await fetch_all(
        connection,
        InviteRow,
        """
        SELECT i.id, i.chart_id, c.title AS chart_title, i.sonolus_id, i.permissions, i.invited_by, i.created_at
        FROM chart_collaborator_invites i JOIN charts c ON c.id = i.chart_id
        WHERE i.chart_id = $1
        ORDER BY i.created_at DESC
        """,
        chart_id,
    )


async def take_invite(connection: Connection, invite_id: UUID, sonolus_id: str) -> InviteRow | None:
    """Delete an invite addressed to an account and return it.

    Args:
        connection: Database connection.
        invite_id: Invite id.
        sonolus_id: Account the invite must be addressed to.

    Returns:
        InviteRow | None: The invite, or None.
    """
    return await fetch_one(
        connection,
        InviteRow,
        """
        WITH taken AS (
            DELETE FROM chart_collaborator_invites WHERE id = $1 AND sonolus_id = $2 RETURNING *
        )
        SELECT t.id, t.chart_id, c.title AS chart_title, t.sonolus_id, t.permissions, t.invited_by, t.created_at
        FROM taken t JOIN charts c ON c.id = t.chart_id
        """,
        invite_id,
        sonolus_id,
    )


async def cancel_invite(connection: Connection, chart_id: str, sonolus_id: str) -> bool:
    """Cancel a pending invite.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        sonolus_id: Invited account id.

    Returns:
        bool: Whether an invite was cancelled.
    """
    deleted = await connection.fetchval(
        "DELETE FROM chart_collaborator_invites WHERE chart_id = $1 AND sonolus_id = $2 RETURNING true",
        chart_id,
        sonolus_id,
    )
    return bool(deleted)


async def add_collaborator(
    connection: Connection,
    chart_id: str,
    sonolus_id: str,
    permissions: list[str],
    added_by: str | None,
) -> None:
    """Add a collaborator, keeping existing ones' permissions merged.

    Args:
        connection: Database connection.
        chart_id: Chart id.
        sonolus_id: Account id.
        permissions: Chart permissions.
        added_by: Account that invited them.
    """
    await connection.execute(
        """
        INSERT INTO chart_collaborators (chart_id, sonolus_id, permissions, added_by)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (chart_id, sonolus_id) DO UPDATE SET permissions = EXCLUDED.permissions
        """,
        chart_id,
        sonolus_id,
        sorted(set(permissions)),
        added_by,
    )
