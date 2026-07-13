from datetime import datetime, timezone

from fastapi import APIRouter, Request, Form, Header

from database import oauth

from helpers.models import OAuthAppWithSecret
from helpers.oauth import (
    ACCESS_TOKEN_PREFIX,
    ACCESS_TOKEN_TTL,
    REFRESH_TOKEN_PREFIX,
    basic_auth_credentials,
    client_authenticated,
    generate_token,
    hash_token,
    oauth_error,
    verify_code_challenge,
)

from typing import Optional

from core import ChartFastAPI

router = APIRouter()


@router.post("/")
async def main(
    request: Request,
    grant_type: str = Form(...),
    client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None),
):
    app: ChartFastAPI = request.app

    basic_id, basic_secret = basic_auth_credentials(authorization)
    client_id = client_id or basic_id
    client_secret = client_secret or basic_secret

    if not client_id:
        return oauth_error("invalid_client", "Missing client credentials.", 401)

    async with app.db_acquire() as conn:
        oauth_app: Optional[OAuthAppWithSecret] = await conn.fetchrow(
            oauth.get_app(client_id)
        )

        if not oauth_app or not client_authenticated(oauth_app, client_secret):
            return oauth_error("invalid_client", "Invalid client credentials.", 401)

        if grant_type == "authorization_code":
            if not code or not redirect_uri:
                return oauth_error("invalid_request", "Missing code or redirect_uri.")

            grant = await conn.fetchrow(
                oauth.consume_authorization_code(hash_token(code))
            )

            if not grant:
                return oauth_error("invalid_grant", "Invalid or already used code.")
            if grant.expires_at < datetime.now(timezone.utc):
                return oauth_error("invalid_grant", "Expired code.")
            if grant.client_id != client_id:
                return oauth_error(
                    "invalid_grant", "Code was issued to another client."
                )
            if grant.redirect_uri != redirect_uri:
                return oauth_error("invalid_grant", "redirect_uri mismatch.")

            # a public client with no challenge on the code proves nothing
            if oauth_app.public and not grant.code_challenge:
                return oauth_error(
                    "invalid_grant", "PKCE is required for public clients."
                )

            if grant.code_challenge and (
                not code_verifier
                or not verify_code_challenge(code_verifier, grant.code_challenge)
            ):
                return oauth_error("invalid_grant", "Invalid code_verifier.")

        elif grant_type == "refresh_token":
            if not refresh_token:
                return oauth_error("invalid_request", "Missing refresh_token.")

            grant = await conn.fetchrow(
                oauth.consume_refresh_token(hash_token(refresh_token), client_id)
            )

            if not grant:
                return oauth_error("invalid_grant", "Invalid or expired refresh_token.")

        else:
            return oauth_error(
                "unsupported_grant_type", f"Unsupported grant: {grant_type}."
            )

        access_token = generate_token(ACCESS_TOKEN_PREFIX)
        new_refresh_token = generate_token(REFRESH_TOKEN_PREFIX)

        await conn.execute(
            oauth.create_token(
                access_token_hash=hash_token(access_token),
                refresh_token_hash=hash_token(new_refresh_token),
                client_id=client_id,
                user_id=grant.user_id,
                scopes=grant.scopes,
            )
        )

    return {
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TOKEN_TTL.total_seconds()),
        "scope": " ".join(grant.scopes),
    }
