from fastapi import APIRouter, Request, HTTPException, status

from database import oauth

from helpers.models import OAuthAuthorizeRequest
from helpers.oauth import (
    AUTHORIZATION_CODE_PREFIX,
    SCOPE_DESCRIPTIONS,
    build_redirect,
    generate_token,
    hash_token,
    parse_scopes,
)
from helpers.session import get_session, Session

from typing import Optional

from core import ChartFastAPI

router = APIRouter()


@router.get("/")
async def app_info(
    request: Request,
    client_id: str,
    redirect_uri: str,
    scope: str,
    response_type: str = "code",
    code_challenge: Optional[str] = None,
    session: Session = get_session(
        enforce_auth=True, enforce_type="external", allow_banned_users=False
    ),
):
    """Consent screen data. The frontend renders this, then POSTs to get a code."""
    app: ChartFastAPI = request.app

    if response_type != "code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported response_type.",
        )

    scopes = parse_scopes(scope)
    if not scopes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid scope."
        )

    async with app.db_acquire() as conn:
        oauth_app = await conn.fetchrow(oauth.get_app(client_id))

    if not oauth_app:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown client_id."
        )
    if redirect_uri not in oauth_app.redirect_uris:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid redirect_uri."
        )
    # fail here rather than after the user has agreed to something we'd refuse
    if oauth_app.public and not code_challenge:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This app must use PKCE (code_challenge).",
        )

    return {
        "client_id": oauth_app.client_id,
        "name": oauth_app.name,
        "description": oauth_app.description,
        "redirect_uri": redirect_uri,
        "scopes": [
            {"scope": scope, "description": SCOPE_DESCRIPTIONS[scope]}
            for scope in scopes
        ],
    }


@router.post("/")
async def authorize(
    request: Request,
    data: OAuthAuthorizeRequest,
    session: Session = get_session(
        enforce_auth=True, enforce_type="external", allow_banned_users=False
    ),
):
    """The user consented. Hand back a code for the app to exchange."""
    app: ChartFastAPI = request.app

    async with app.db_acquire() as conn:
        oauth_app = await conn.fetchrow(oauth.get_app(data.client_id))

        if not oauth_app:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown client_id."
            )
        if data.redirect_uri not in oauth_app.redirect_uris:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid redirect_uri."
            )
        if oauth_app.public and not data.code_challenge:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This app must use PKCE (code_challenge).",
            )

        code = generate_token(AUTHORIZATION_CODE_PREFIX)
        await conn.execute(
            oauth.create_authorization_code(
                code_hash=hash_token(code),
                client_id=data.client_id,
                user_id=session.sonolus_id,
                scopes=data.scopes,
                redirect_uri=data.redirect_uri,
                code_challenge=data.code_challenge,
            )
        )

    params: dict[str, str] = {"code": code}
    if data.state:
        params["state"] = data.state

    return {
        "code": code,
        "state": data.state,
        "redirect_uri": build_redirect(data.redirect_uri, params),
    }
