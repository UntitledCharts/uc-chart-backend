from datetime import datetime

from pydantic import BaseModel

from app.database.accounts import ACCOUNT_COLUMNS, AccountRow
from app.database.core import Connection, fetch_all, fetch_one, fetch_value


class OAuthAppRow(BaseModel):
    """A registered third-party OAuth app.

    Attributes:
        client_id: Public client id.
        client_secret_hash: SHA-256 hex of the client secret, None for public apps.
        name: App name shown on the consent page.
        description: App description.
        owner_id: Account that owns the app.
        redirect_uris: Exact redirect URIs the app may use.
        public: Whether the app can't keep a secret and must use PKCE.
        created_at: Registration time.
        updated_at: Last update time.
    """

    client_id: str
    client_secret_hash: str | None = None
    name: str
    description: str | None = None
    owner_id: str | None = None
    redirect_uris: list[str]
    public: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AuthorizationCodeRow(BaseModel):
    """A consumed authorization code.

    Attributes:
        client_id: App the code was issued to.
        user_id: Account that approved the app.
        scopes: Approved scopes.
        redirect_uri: Redirect URI the code was issued for.
        code_challenge: PKCE S256 challenge, if any.
        expires_at: Expiry time.
    """

    client_id: str
    user_id: str
    scopes: list[str]
    redirect_uri: str
    code_challenge: str | None = None
    expires_at: datetime


class GrantRow(BaseModel):
    """The account and scopes behind a token.

    Attributes:
        client_id: App holding the token.
        user_id: Account the token acts for.
        scopes: Granted scopes.
    """

    client_id: str
    user_id: str
    scopes: list[str]


class AccessTokenPrincipal(BaseModel):
    """An access token's account and scopes.

    Attributes:
        account: Account the token acts for.
        client_id: App holding the token.
        scopes: Granted scopes.
    """

    account: AccountRow
    client_id: str
    scopes: list[str]


class AuthorizationRow(BaseModel):
    """An app the account has authorized.

    Attributes:
        client_id: App client id.
        name: App name.
        description: App description.
        scopes: Union of scopes across the app's live tokens.
        authorized_at: First token issue time.
        last_used_at: Latest token issue or refresh time.
    """

    client_id: str
    name: str
    description: str | None = None
    scopes: list[str]
    authorized_at: datetime
    last_used_at: datetime


async def get_app(connection: Connection, client_id: str) -> OAuthAppRow | None:
    """Fetch an OAuth app.

    Args:
        connection: Database connection.
        client_id: Client id.

    Returns:
        OAuthAppRow | None: The app, or None.
    """
    return await fetch_one(
        connection,
        OAuthAppRow,
        "SELECT * FROM oauth_apps WHERE client_id = $1",
        client_id,
    )


async def list_apps(connection: Connection) -> list[OAuthAppRow]:
    """List every OAuth app, newest first.

    Args:
        connection: Database connection.

    Returns:
        list[OAuthAppRow]: The apps.
    """
    return await fetch_all(
        connection, OAuthAppRow, "SELECT * FROM oauth_apps ORDER BY created_at DESC"
    )


async def create_app(
    connection: Connection,
    client_id: str,
    client_secret_hash: str | None,
    name: str,
    redirect_uris: list[str],
    description: str | None,
    public: bool,
    owner_id: str | None,
) -> OAuthAppRow:
    """Register an OAuth app.

    Args:
        connection: Database connection.
        client_id: New client id.
        client_secret_hash: SHA-256 hex of the secret, None for public apps.
        name: App name.
        redirect_uris: Allowed redirect URIs.
        description: App description.
        public: Whether the app is public.
        owner_id: Owning account.

    Returns:
        OAuthAppRow: The app.
    """
    row = await fetch_one(
        connection,
        OAuthAppRow,
        """
        INSERT INTO oauth_apps (client_id, client_secret_hash, public, name, description, redirect_uris, owner_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING *
        """,
        client_id,
        client_secret_hash,
        public,
        name,
        description,
        redirect_uris,
        owner_id,
    )
    assert row is not None
    return row


async def set_client_secret(
    connection: Connection, client_id: str, client_secret_hash: str
) -> OAuthAppRow | None:
    """Replace an app's client secret.

    Args:
        connection: Database connection.
        client_id: Client id.
        client_secret_hash: SHA-256 hex of the new secret.

    Returns:
        OAuthAppRow | None: The app, or None when unknown.
    """
    return await fetch_one(
        connection,
        OAuthAppRow,
        """
        UPDATE oauth_apps SET client_secret_hash = $2, updated_at = CURRENT_TIMESTAMP
        WHERE client_id = $1 RETURNING *
        """,
        client_id,
        client_secret_hash,
    )


async def set_redirect_uris(
    connection: Connection, client_id: str, redirect_uris: list[str]
) -> OAuthAppRow | None:
    """Replace an app's redirect URIs.

    Args:
        connection: Database connection.
        client_id: Client id.
        redirect_uris: New redirect URIs.

    Returns:
        OAuthAppRow | None: The app, or None when unknown.
    """
    return await fetch_one(
        connection,
        OAuthAppRow,
        """
        UPDATE oauth_apps SET redirect_uris = $2, updated_at = CURRENT_TIMESTAMP
        WHERE client_id = $1 RETURNING *
        """,
        client_id,
        redirect_uris,
    )


async def delete_app(connection: Connection, client_id: str) -> bool:
    """Delete an app and its codes and tokens.

    Args:
        connection: Database connection.
        client_id: Client id.

    Returns:
        bool: Whether an app was deleted.
    """
    deleted = await fetch_value(
        connection,
        "DELETE FROM oauth_apps WHERE client_id = $1 RETURNING true",
        client_id,
    )
    return bool(deleted)


async def lock_grants(connection: Connection, user_id: str) -> None:
    """Make other code, token and revocation changes of this account wait for the transaction.

    Args:
        connection: Database connection inside a transaction.
        user_id: Account id.
    """
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended('oauth:' || $1::text, 0))", user_id
    )


async def create_authorization_code(
    connection: Connection,
    code_hash: str,
    client_id: str,
    user_id: str,
    scopes: list[str],
    redirect_uri: str,
    code_challenge: str | None,
    lifetime_seconds: int,
) -> None:
    """Store an authorization code under the account's grant lock.

    Args:
        connection: Database connection inside a transaction.
        code_hash: SHA-256 hex of the code.
        client_id: App the code is for.
        user_id: Approving account.
        scopes: Approved scopes.
        redirect_uri: Redirect URI the code is bound to.
        code_challenge: PKCE S256 challenge, if any.
        lifetime_seconds: Code lifetime.
    """
    await lock_grants(connection, user_id)
    await connection.execute(
        """
        INSERT INTO oauth_authorization_codes
            (code_hash, client_id, user_id, scopes, redirect_uri, code_challenge, expires_at)
        VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP + make_interval(secs => $7))
        """,
        code_hash,
        client_id,
        user_id,
        list(dict.fromkeys(scopes)),
        redirect_uri,
        code_challenge,
        lifetime_seconds,
    )


async def consume_authorization_code(
    connection: Connection, code_hash: str
) -> AuthorizationCodeRow | None:
    """Delete an authorization code under its account's grant lock and return it, so it works once.

    Args:
        connection: Database connection inside a transaction.
        code_hash: SHA-256 hex of the code.

    Returns:
        AuthorizationCodeRow | None: The code, or None when unknown or already used.
    """
    user_id = await fetch_value(
        connection, "SELECT user_id FROM oauth_authorization_codes WHERE code_hash = $1", code_hash
    )
    if user_id is None:
        return None
    await lock_grants(connection, user_id)
    return await fetch_one(
        connection,
        AuthorizationCodeRow,
        """
        DELETE FROM oauth_authorization_codes WHERE code_hash = $1
        RETURNING client_id, user_id, scopes, redirect_uri, code_challenge, expires_at
        """,
        code_hash,
    )


async def create_token(
    connection: Connection,
    access_token_hash: str,
    refresh_token_hash: str,
    client_id: str,
    user_id: str,
    scopes: list[str],
    access_seconds: int,
    refresh_seconds: int,
) -> None:
    """Store an access and refresh token pair.

    Args:
        connection: Database connection.
        access_token_hash: SHA-256 hex of the access token.
        refresh_token_hash: SHA-256 hex of the refresh token.
        client_id: App holding the tokens.
        user_id: Account they act for.
        scopes: Granted scopes.
        access_seconds: Access token lifetime.
        refresh_seconds: Refresh token lifetime.
    """
    await connection.execute(
        """
        INSERT INTO oauth_tokens
            (access_token_hash, refresh_token_hash, client_id, user_id, scopes, access_expires_at, refresh_expires_at)
        VALUES ($1, $2, $3, $4, $5,
                CURRENT_TIMESTAMP + make_interval(secs => $6),
                CURRENT_TIMESTAMP + make_interval(secs => $7))
        """,
        access_token_hash,
        refresh_token_hash,
        client_id,
        user_id,
        list(scopes),
        access_seconds,
        refresh_seconds,
    )


async def get_access_token_principal(
    connection: Connection, access_token_hash: str
) -> AccessTokenPrincipal | None:
    """Resolve a live access token.

    Args:
        connection: Database connection.
        access_token_hash: SHA-256 hex of the access token.

    Returns:
        AccessTokenPrincipal | None: The account and scopes, or None when invalid.
    """
    record = await connection.fetchrow(
        f"""
        SELECT t.client_id AS t_client_id, t.scopes AS t_scopes, {ACCOUNT_COLUMNS}
        FROM oauth_tokens t
        JOIN accounts a ON a.sonolus_id = t.user_id
        WHERE t.access_token_hash = $1 AND t.revoked = false AND t.access_expires_at > CURRENT_TIMESTAMP
        """,
        access_token_hash,
    )
    if record is None:
        return None
    data = dict(record)
    client_id = data.pop("t_client_id")
    scopes = list(data.pop("t_scopes"))
    return AccessTokenPrincipal(
        account=AccountRow.model_validate(data), client_id=client_id, scopes=scopes
    )


async def rotate_refresh_token(
    connection: Connection, refresh_token_hash: str, client_id: str
) -> GrantRow | None:
    """Revoke a live refresh token's pair under its account's grant lock and return its grant.

    Args:
        connection: Database connection inside a transaction.
        refresh_token_hash: SHA-256 hex of the refresh token.
        client_id: App presenting it.

    Returns:
        GrantRow | None: The grant, or None when the token is not live.
    """
    user_id = await fetch_value(
        connection,
        "SELECT user_id FROM oauth_tokens WHERE refresh_token_hash = $1 AND client_id = $2",
        refresh_token_hash,
        client_id,
    )
    if user_id is None:
        return None
    await lock_grants(connection, user_id)
    return await fetch_one(
        connection,
        GrantRow,
        """
        UPDATE oauth_tokens SET revoked = true
        WHERE refresh_token_hash = $1 AND client_id = $2 AND revoked = false
          AND refresh_expires_at > CURRENT_TIMESTAMP
        RETURNING client_id, user_id, scopes
        """,
        refresh_token_hash,
        client_id,
    )


async def find_reused_refresh_token(
    connection: Connection, refresh_token_hash: str, client_id: str
) -> GrantRow | None:
    """Find an already revoked refresh token being presented again.

    Args:
        connection: Database connection.
        refresh_token_hash: SHA-256 hex of the refresh token.
        client_id: App presenting it.

    Returns:
        GrantRow | None: The grant of the reused token, or None.
    """
    return await fetch_one(
        connection,
        GrantRow,
        """
        SELECT client_id, user_id, scopes FROM oauth_tokens
        WHERE refresh_token_hash = $1 AND client_id = $2 AND revoked = true
        """,
        refresh_token_hash,
        client_id,
    )


async def revoke_token(connection: Connection, token_hash: str, client_id: str) -> None:
    """Revoke the token pair containing a token.

    Args:
        connection: Database connection.
        token_hash: SHA-256 hex of the access or refresh token.
        client_id: App the token belongs to.
    """
    await connection.execute(
        """
        UPDATE oauth_tokens SET revoked = true
        WHERE (access_token_hash = $1 OR refresh_token_hash = $1) AND client_id = $2
        """,
        token_hash,
        client_id,
    )


async def revoke_authorization(connection: Connection, user_id: str, client_id: str) -> bool:
    """Revoke every token and pending code an app holds for an account, under its grant lock.

    Args:
        connection: Database connection inside a transaction.
        user_id: Account id.
        client_id: App client id.

    Returns:
        bool: Whether the app held any live token or pending code.
    """
    await lock_grants(connection, user_id)
    revoked = await connection.execute(
        "UPDATE oauth_tokens SET revoked = true WHERE user_id = $1 AND client_id = $2 AND revoked = false",
        user_id,
        client_id,
    )
    deleted = await connection.execute(
        "DELETE FROM oauth_authorization_codes WHERE user_id = $1 AND client_id = $2",
        user_id,
        client_id,
    )
    return int(revoked.split()[-1]) + int(deleted.split()[-1]) > 0


async def revoke_all_for_account(connection: Connection, user_id: str) -> None:
    """Revoke every token and pending code of an account under its grant lock, for example on ban.

    Args:
        connection: Database connection inside a transaction.
        user_id: Account id.
    """
    await lock_grants(connection, user_id)
    await connection.execute(
        "UPDATE oauth_tokens SET revoked = true WHERE user_id = $1 AND revoked = false",
        user_id,
    )
    await connection.execute("DELETE FROM oauth_authorization_codes WHERE user_id = $1", user_id)


async def list_authorizations(connection: Connection, user_id: str) -> list[AuthorizationRow]:
    """List the apps an account has live tokens for.

    Args:
        connection: Database connection.
        user_id: Account id.

    Returns:
        list[AuthorizationRow]: One row per app, most recently used first.
    """
    return await fetch_all(
        connection,
        AuthorizationRow,
        """
        SELECT
            app.client_id,
            app.name,
            app.description,
            COALESCE(array_remove(array_agg(DISTINCT scope), NULL), '{}') AS scopes,
            MIN(t.created_at) AS authorized_at,
            MAX(t.created_at) AS last_used_at
        FROM oauth_tokens t
        JOIN oauth_apps app ON app.client_id = t.client_id
        LEFT JOIN LATERAL unnest(t.scopes) AS scope ON true
        WHERE t.user_id = $1 AND t.revoked = false AND t.refresh_expires_at > CURRENT_TIMESTAMP
        GROUP BY app.client_id, app.name, app.description
        ORDER BY MAX(t.created_at) DESC
        """,
        user_id,
    )


async def delete_expired_codes(connection: Connection, limit: int) -> int:
    """Delete a batch of expired authorization codes.

    Args:
        connection: Database connection.
        limit: Most rows deleted.

    Returns:
        int: Number of rows deleted.
    """
    result = await connection.execute(
        """
        DELETE FROM oauth_authorization_codes WHERE code_hash IN (
            SELECT code_hash FROM oauth_authorization_codes WHERE expires_at <= CURRENT_TIMESTAMP LIMIT $1
        )
        """,
        limit,
    )
    return int(result.split()[-1])


async def delete_expired_tokens(connection: Connection, limit: int) -> int:
    """Delete a batch of token pairs whose refresh token expired.

    Args:
        connection: Database connection.
        limit: Most rows deleted.

    Returns:
        int: Number of rows deleted.
    """
    result = await connection.execute(
        """
        DELETE FROM oauth_tokens WHERE id IN (
            SELECT id FROM oauth_tokens WHERE refresh_expires_at <= CURRENT_TIMESTAMP LIMIT $1
        )
        """,
        limit,
    )
    return int(result.split()[-1])
