from datetime import datetime

from pydantic import BaseModel

from app.database.core import Connection, fetch_one, fetch_value


class LoginRequestRow(BaseModel):
    """A pending website login request.

    Attributes:
        id_key: Login request id shown in the login link.
        session_key: Session token once approved in Sonolus.
        expires_at: Expiry time.
    """

    id_key: str
    session_key: str | None = None
    expires_at: datetime


async def create_login_request(
    connection: Connection, id_key: str, lifetime_seconds: int
) -> LoginRequestRow:
    """Create a login request.

    Args:
        connection: Database connection.
        id_key: Login request id.
        lifetime_seconds: Time the link stays valid.

    Returns:
        LoginRequestRow: The request.
    """
    row = await fetch_one(
        connection,
        LoginRequestRow,
        """
        INSERT INTO external_login_ids (id_key, expires_at)
        VALUES ($1, CURRENT_TIMESTAMP + make_interval(secs => $2))
        RETURNING id_key, session_key, expires_at
        """,
        id_key,
        lifetime_seconds,
    )
    assert row is not None
    return row


async def lock_pending_login_request(connection: Connection, id_key: str) -> LoginRequestRow | None:
    """Lock a login request that is unexpired and not approved yet.

    Args:
        connection: Database connection inside a transaction.
        id_key: Login request id.

    Returns:
        LoginRequestRow | None: The request, or None when it is expired, used or unknown.
    """
    return await fetch_one(
        connection,
        LoginRequestRow,
        """
        SELECT id_key, session_key, expires_at FROM external_login_ids
        WHERE id_key = $1 AND expires_at > CURRENT_TIMESTAMP AND session_key IS NULL
        FOR UPDATE
        """,
        id_key,
    )


async def approve_login_request(
    connection: Connection, id_key: str, session_key: str, pickup_seconds: int
) -> None:
    """Attach a session to a locked login request.

    Args:
        connection: Database connection inside the locking transaction.
        id_key: Login request id.
        session_key: Session token for the website to collect.
        pickup_seconds: Time the website has to collect it.
    """
    await connection.execute(
        """
        UPDATE external_login_ids
        SET session_key = $2, expires_at = CURRENT_TIMESTAMP + make_interval(secs => $3)
        WHERE id_key = $1
        """,
        id_key,
        session_key,
        pickup_seconds,
    )


async def get_login_request(connection: Connection, id_key: str) -> LoginRequestRow | None:
    """Fetch an unexpired login request.

    Args:
        connection: Database connection.
        id_key: Login request id.

    Returns:
        LoginRequestRow | None: The request, or None.
    """
    return await fetch_one(
        connection,
        LoginRequestRow,
        """
        SELECT id_key, session_key, expires_at FROM external_login_ids
        WHERE id_key = $1 AND expires_at > CURRENT_TIMESTAMP
        """,
        id_key,
    )


async def take_approved_login_request(connection: Connection, id_key: str) -> str | None:
    """Delete an approved login request and return its session token.

    Args:
        connection: Database connection.
        id_key: Login request id.

    Returns:
        str | None: The session token, or None when not approved or already taken.
    """
    return await fetch_value(
        connection,
        """
        DELETE FROM external_login_ids
        WHERE id_key = $1 AND expires_at > CURRENT_TIMESTAMP AND session_key IS NOT NULL
        RETURNING session_key
        """,
        id_key,
    )


async def delete_expired_login_requests(connection: Connection, limit: int) -> int:
    """Delete a batch of expired login requests.

    Args:
        connection: Database connection.
        limit: Most rows deleted.

    Returns:
        int: Number of rows deleted.
    """
    result = await connection.execute(
        """
        DELETE FROM external_login_ids WHERE id_key IN (
            SELECT id_key FROM external_login_ids WHERE expires_at <= CURRENT_TIMESTAMP LIMIT $1
        )
        """,
        limit,
    )
    return int(result.split()[-1])
