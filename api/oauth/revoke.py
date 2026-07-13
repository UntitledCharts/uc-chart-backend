from fastapi import APIRouter, Request, Form, Header

from database import oauth

from helpers.models import OAuthAppWithSecret
from helpers.oauth import (
    basic_auth_credentials,
    hash_token,
    oauth_error,
    verify_hash,
)

from typing import Optional

from core import ChartFastAPI

router = APIRouter()


@router.post("/")
async def main(
    request: Request,
    token: str = Form(...),
    client_id: Optional[str] = Form(None),
    client_secret: Optional[str] = Form(None),
    authorization: Optional[str] = Header(None),
):
    app: ChartFastAPI = request.app

    basic_id, basic_secret = basic_auth_credentials(authorization)
    client_id = client_id or basic_id
    client_secret = client_secret or basic_secret

    if not client_id or not client_secret:
        return oauth_error("invalid_client", "Missing client credentials.", 401)

    async with app.db_acquire() as conn:
        oauth_app: Optional[OAuthAppWithSecret] = await conn.fetchrow(
            oauth.get_app(client_id)
        )

        if not oauth_app or not verify_hash(
            client_secret, oauth_app.client_secret_hash
        ):
            return oauth_error("invalid_client", "Invalid client credentials.", 401)

        # access and refresh token of a pair are revoked together
        await conn.execute(oauth.revoke_token(hash_token(token), client_id))

    return {}
