from datetime import datetime, timezone

from typing import Optional

from database.query import ExecutableQuery, SelectQuery
from helpers.models import (
    OAuthAccount,
    OAuthApp,
    OAuthAppWithSecret,
    OAuthAuthorization,
    OAuthCode,
    OAuthGrant,
)
from helpers.oauth import (
    ACCESS_TOKEN_TTL,
    AUTHORIZATION_CODE_TTL,
    REFRESH_TOKEN_TTL,
    OAuthScope,
)


def create_app(
    client_id: str,
    client_secret_hash: Optional[str],
    name: str,
    redirect_uris: list[str],
    description: Optional[str] = None,
    public: bool = False,
    owner_id: Optional[str] = None,
) -> SelectQuery[OAuthApp]:
    return SelectQuery(
        OAuthApp,
        """
            INSERT INTO oauth_apps (client_id, client_secret_hash, public, name, description, redirect_uris, owner_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING client_id, name, description, public, owner_id, redirect_uris, created_at, updated_at;
        """,
        client_id,
        client_secret_hash,
        public,
        name,
        description,
        redirect_uris,
        owner_id,
    )


def get_app(client_id: str) -> SelectQuery[OAuthAppWithSecret]:
    return SelectQuery(
        OAuthAppWithSecret,
        """
            SELECT * FROM oauth_apps
            WHERE client_id = $1
            LIMIT 1;
        """,
        client_id,
    )


def list_apps() -> SelectQuery[OAuthApp]:
    return SelectQuery(
        OAuthApp,
        """
            SELECT client_id, name, description, public, owner_id, redirect_uris, created_at, updated_at
            FROM oauth_apps
            ORDER BY created_at DESC;
        """,
    )


def regenerate_secret(client_id: str, client_secret_hash: str) -> SelectQuery[OAuthApp]:
    return SelectQuery(
        OAuthApp,
        """
            UPDATE oauth_apps
            SET client_secret_hash = $2, updated_at = CURRENT_TIMESTAMP
            WHERE client_id = $1
            RETURNING client_id, name, description, public, owner_id, redirect_uris, created_at, updated_at;
        """,
        client_id,
        client_secret_hash,
    )


def set_redirect_uris(
    client_id: str, redirect_uris: list[str]
) -> SelectQuery[OAuthApp]:
    return SelectQuery(
        OAuthApp,
        """
            UPDATE oauth_apps
            SET redirect_uris = $2, updated_at = CURRENT_TIMESTAMP
            WHERE client_id = $1
            RETURNING client_id, name, description, public, owner_id, redirect_uris, created_at, updated_at;
        """,
        client_id,
        redirect_uris,
    )


def delete_app(client_id: str) -> SelectQuery[OAuthApp]:
    return SelectQuery(
        OAuthApp,
        """
            DELETE FROM oauth_apps
            WHERE client_id = $1
            RETURNING client_id, name, description, public, owner_id, redirect_uris, created_at, updated_at;
        """,
        client_id,
    )


def create_authorization_code(
    code_hash: str,
    client_id: str,
    user_id: str,
    scopes: list[OAuthScope],
    redirect_uri: str,
    code_challenge: Optional[str] = None,
) -> ExecutableQuery:
    expires_at = datetime.now(timezone.utc) + AUTHORIZATION_CODE_TTL

    return ExecutableQuery(
        """
            INSERT INTO oauth_authorization_codes
                (code_hash, client_id, user_id, scopes, redirect_uri, code_challenge, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7);
        """,
        code_hash,
        client_id,
        user_id,
        list(scopes),
        redirect_uri,
        code_challenge,
        expires_at,
    )


def consume_authorization_code(code_hash: str) -> SelectQuery[OAuthCode]:
    # single use: the row is gone whether or not it validates
    return SelectQuery(
        OAuthCode,
        """
            DELETE FROM oauth_authorization_codes
            WHERE code_hash = $1
            RETURNING client_id, user_id, scopes, redirect_uri, code_challenge, expires_at;
        """,
        code_hash,
    )


def create_token(
    access_token_hash: str,
    refresh_token_hash: str,
    client_id: str,
    user_id: str,
    scopes: list[OAuthScope],
) -> ExecutableQuery:
    now = datetime.now(timezone.utc)

    return ExecutableQuery(
        """
            INSERT INTO oauth_tokens
                (access_token_hash, refresh_token_hash, client_id, user_id, scopes,
                 access_expires_at, refresh_expires_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7);
        """,
        access_token_hash,
        refresh_token_hash,
        client_id,
        user_id,
        list(scopes),
        now + ACCESS_TOKEN_TTL,
        now + REFRESH_TOKEN_TTL,
    )


def get_account_from_access_token(
    access_token_hash: str,
) -> SelectQuery[OAuthAccount]:
    return SelectQuery(
        OAuthAccount,
        """
            SELECT a.*, t.client_id, t.scopes
            FROM oauth_tokens t
            JOIN accounts a ON a.sonolus_id = t.user_id
            WHERE t.access_token_hash = $1
                AND t.revoked = false
                AND t.access_expires_at > CURRENT_TIMESTAMP
            LIMIT 1;
        """,
        access_token_hash,
    )


def consume_refresh_token(
    refresh_token_hash: str, client_id: str
) -> SelectQuery[OAuthGrant]:
    # rotation: the old pair dies here, the caller inserts the new one
    return SelectQuery(
        OAuthGrant,
        """
            UPDATE oauth_tokens
            SET revoked = true
            WHERE refresh_token_hash = $1
                AND client_id = $2
                AND revoked = false
                AND refresh_expires_at > CURRENT_TIMESTAMP
            RETURNING client_id, user_id, scopes;
        """,
        refresh_token_hash,
        client_id,
    )


def revoke_token(token_hash: str, client_id: str) -> ExecutableQuery:
    return ExecutableQuery(
        """
            UPDATE oauth_tokens
            SET revoked = true
            WHERE (access_token_hash = $1 OR refresh_token_hash = $1)
                AND client_id = $2;
        """,
        token_hash,
        client_id,
    )


def get_authorizations(user_id: str) -> SelectQuery[OAuthAuthorization]:
    # one row per app, scopes unioned across whatever live tokens it holds
    return SelectQuery(
        OAuthAuthorization,
        """
            SELECT
                app.client_id,
                app.name,
                app.description,
                (
                    SELECT COALESCE(array_agg(DISTINCT scope), '{}')
                    FROM oauth_tokens live, unnest(live.scopes) AS scope
                    WHERE live.user_id = t.user_id
                        AND live.client_id = app.client_id
                        AND live.revoked = false
                        AND live.refresh_expires_at > CURRENT_TIMESTAMP
                ) AS scopes,
                MIN(t.created_at) AS authorized_at,
                MAX(t.created_at) AS last_used_at
            FROM oauth_tokens t
            JOIN oauth_apps app ON app.client_id = t.client_id
            WHERE t.user_id = $1
                AND t.revoked = false
                AND t.refresh_expires_at > CURRENT_TIMESTAMP
            GROUP BY app.client_id, app.name, app.description, t.user_id
            ORDER BY MAX(t.created_at) DESC;
        """,
        user_id,
    )


def revoke_authorization(user_id: str, client_id: str) -> SelectQuery[OAuthGrant]:
    return SelectQuery(
        OAuthGrant,
        """
            UPDATE oauth_tokens
            SET revoked = true
            WHERE user_id = $1
                AND client_id = $2
                AND revoked = false
            RETURNING client_id, user_id, scopes;
        """,
        user_id,
        client_id,
    )
