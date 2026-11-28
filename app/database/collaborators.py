import json
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
    """Add a collaborator, leaving an existing collaborator and their permissions untouched.

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
        ON CONFLICT (chart_id, sonolus_id) DO NOTHING
        """,
        chart_id,
        sonolus_id,
        sorted(set(permissions)),
        added_by,
    )


async def take_all_for_account(connection: Connection, sonolus_id: str) -> str | None:
    """Remove every collaboration and pending invite of an account.

    Args:
        connection: Database connection inside a transaction.
        sonolus_id: Account id.

    Returns:
        str | None: JSON snapshot of what was removed, for restoring it later, or None when the
            account had nothing.
    """
    collaborations = await connection.fetch(
        """
        DELETE FROM chart_collaborators WHERE sonolus_id = $1
        RETURNING chart_id, permissions, added_by, added_at
        """,
        sonolus_id,
    )
    invites = await connection.fetch(
        """
        DELETE FROM chart_collaborator_invites WHERE sonolus_id = $1
        RETURNING chart_id, permissions, invited_by, created_at
        """,
        sonolus_id,
    )
    if not collaborations and not invites:
        return None
    return json.dumps(
        {
            "collaborations": [
                {
                    "chart_id": row["chart_id"],
                    "permissions": list(row["permissions"]),
                    "added_by": row["added_by"],
                    "added_at": row["added_at"].isoformat(),
                }
                for row in collaborations
            ],
            "invites": [
                {
                    "chart_id": row["chart_id"],
                    "permissions": list(row["permissions"]),
                    "invited_by": row["invited_by"],
                    "created_at": row["created_at"].isoformat(),
                }
                for row in invites
            ],
        }
    )


async def restore_for_account(connection: Connection, sonolus_id: str, snapshot: str) -> int:
    """Put back collaborations and invites removed by take_all_for_account.

    Charts that no longer exist are skipped, and nothing already present is overwritten.

    Args:
        connection: Database connection inside a transaction.
        sonolus_id: Account id.
        snapshot: JSON snapshot returned by take_all_for_account.

    Returns:
        int: Collaborations and invites restored.
    """
    data = json.loads(snapshot)
    restored = await connection.fetchval(
        """
        WITH added AS (
            INSERT INTO chart_collaborators (chart_id, sonolus_id, permissions, added_by, added_at)
            SELECT s.chart_id, $1, s.permissions, ab.sonolus_id, s.added_at
            FROM jsonb_to_recordset($2::jsonb)
                AS s(chart_id text, permissions text[], added_by text, added_at timestamptz)
            JOIN charts c ON c.id = s.chart_id AND c.author <> $1
            LEFT JOIN accounts ab ON ab.sonolus_id = s.added_by
            ON CONFLICT (chart_id, sonolus_id) DO NOTHING
            RETURNING 1
        )
        SELECT COUNT(*) FROM added
        """,
        sonolus_id,
        json.dumps(data.get("collaborations", [])),
    )
    invited = await connection.fetchval(
        """
        WITH added AS (
            INSERT INTO chart_collaborator_invites (chart_id, sonolus_id, permissions, invited_by, created_at)
            SELECT s.chart_id, $1, s.permissions, ib.sonolus_id, s.created_at
            FROM jsonb_to_recordset($2::jsonb)
                AS s(chart_id text, permissions text[], invited_by text, created_at timestamptz)
            JOIN charts c ON c.id = s.chart_id AND c.author <> $1
            LEFT JOIN accounts ib ON ib.sonolus_id = s.invited_by
            WHERE NOT EXISTS (
                SELECT 1 FROM chart_collaborators cc WHERE cc.chart_id = s.chart_id AND cc.sonolus_id = $1
            )
            ON CONFLICT (chart_id, sonolus_id) DO NOTHING
            RETURNING 1
        )
        SELECT COUNT(*) FROM added
        """,
        sonolus_id,
        json.dumps(data.get("invites", [])),
    )
    return int(restored or 0) + int(invited or 0)
