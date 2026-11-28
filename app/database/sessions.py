from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.database.accounts import ACCOUNT_COLUMNS, AccountRow
from app.database.core import Connection, fetch_all, fetch_one, fetch_value

SessionType = Literal["game", "website"]


class SessionRow(BaseModel):
    """A login session.

    Attributes:
        id: Session id.
        sonolus_id: Account the session belongs to.
        type: Whether the session is in-game or on the website.
        created_at: Creation time.
        expires_at: Expiry time.
    """

    id: UUID
    sonolus_id: str
    type: SessionType
    created_at: datetime
    expires_at: datetime


class SessionWithAccount(BaseModel):
    """A session together with its account.

    Attributes:
        session: The session.
        account: The account it belongs to.
    """

    session: SessionRow
    account: AccountRow


async def create_session(
    connection: Connection,
    sonolus_id: str,
    session_type: SessionType,
    token_hash: str,
    lifetime_seconds: int,
    max_per_type: int,
) -> SessionRow:
    """Create a session, evicting the account's oldest ones beyond the cap.

    Args:
        connection: Database connection inside a transaction.
        sonolus_id: Account id.
        session_type: Session type.
        token_hash: SHA-256 hex of the session token.
        lifetime_seconds: Session lifetime.
        max_per_type: Sessions kept per account and type.

    Returns:
        SessionRow: The new session.
    """
    await connection.execute("SELECT 1 FROM accounts WHERE sonolus_id = $1 FOR UPDATE", sonolus_id)
    await connection.execute(
        """
        DELETE FROM sessions
        WHERE sonolus_id = $1 AND type = $2
          AND (expires_at <= CURRENT_TIMESTAMP OR id IN (
              SELECT id FROM sessions
              WHERE sonolus_id = $1 AND type = $2 AND expires_at > CURRENT_TIMESTAMP
              ORDER BY expires_at DESC
              OFFSET $3
          ))
        """,
        sonolus_id,
        session_type,
        max(max_per_type - 1, 0),
    )
    row = await fetch_one(
        connection,
        SessionRow,
        """
        INSERT INTO sessions (sonolus_id, type, token_hash, expires_at)
        VALUES ($1, $2, $3, CURRENT_TIMESTAMP + make_interval(secs => $4))
        RETURNING id, sonolus_id, type, created_at, expires_at
        """,
        sonolus_id,
        session_type,
        token_hash,
        lifetime_seconds,
    )
    assert row is not None
    return row


async def get_session_by_token_hash(
    connection: Connection, token_hash: str
) -> SessionWithAccount | None:
    """Resolve a live session and its account from a token hash.

    Args:
        connection: Database connection.
        token_hash: SHA-256 hex of the session token.

    Returns:
        SessionWithAccount | None: The session and account, or None when expired or unknown.
    """
    record = await connection.fetchrow(
        f"""
        SELECT s.id AS s_id, s.sonolus_id AS s_sonolus_id, s.type AS s_type,
               s.created_at AS s_created_at, s.expires_at AS s_expires_at,
               {ACCOUNT_COLUMNS}
        FROM sessions s
        JOIN accounts a ON a.sonolus_id = s.sonolus_id
        WHERE s.token_hash = $1 AND s.expires_at > CURRENT_TIMESTAMP
        """,
        token_hash,
    )
    if record is None:
        return None
    data = dict(record)
    session = SessionRow(
        id=data.pop("s_id"),
        sonolus_id=data.pop("s_sonolus_id"),
        type=data.pop("s_type"),
        created_at=data.pop("s_created_at"),
        expires_at=data.pop("s_expires_at"),
    )
    return SessionWithAccount(session=session, account=AccountRow.model_validate(data))


async def list_sessions(connection: Connection, sonolus_id: str) -> list[SessionRow]:
    """List an account's live sessions, newest first.

    Args:
        connection: Database connection.
        sonolus_id: Account id.

    Returns:
        list[SessionRow]: The sessions.
    """
    return await fetch_all(
        connection,
        SessionRow,
        """
        SELECT id, sonolus_id, type, created_at, expires_at FROM sessions
        WHERE sonolus_id = $1 AND expires_at > CURRENT_TIMESTAMP
        ORDER BY created_at DESC
        """,
        sonolus_id,
    )


async def delete_session(connection: Connection, session_id: UUID, sonolus_id: str) -> bool:
    """Delete one of an account's sessions.

    Args:
        connection: Database connection.
        session_id: Session id.
        sonolus_id: Account id the session must belong to.

    Returns:
        bool: Whether a session was deleted.
    """
    deleted = await fetch_value(
        connection,
        "DELETE FROM sessions WHERE id = $1 AND sonolus_id = $2 RETURNING true",
        session_id,
        sonolus_id,
    )
    return bool(deleted)


async def delete_all_sessions(connection: Connection, sonolus_id: str) -> int:
    """Delete every session of an account.

    Args:
        connection: Database connection.
        sonolus_id: Account id.

    Returns:
        int: Number of sessions deleted.
    """
    result = await connection.execute("DELETE FROM sessions WHERE sonolus_id = $1", sonolus_id)
    return int(result.split()[-1])


async def delete_expired_sessions(connection: Connection, limit: int) -> int:
    """Delete a batch of expired sessions.

    Args:
        connection: Database connection.
        limit: Most rows deleted.

    Returns:
        int: Number of sessions deleted.
    """
    result = await connection.execute(
        """
        DELETE FROM sessions WHERE id IN (
            SELECT id FROM sessions WHERE expires_at <= CURRENT_TIMESTAMP LIMIT $1
        )
        """,
        limit,
    )
    return int(result.split()[-1])
