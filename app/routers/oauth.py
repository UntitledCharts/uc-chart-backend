import base64
import binascii
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import APIRouter, Depends, Form, Header, Query
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator

from app.auth.catalog import ALL_SCOPES, SCOPE_DESCRIPTIONS, OAuthScope, parse_scopes
from app.auth.principal import Principal, auth
from app.auth.tokens import (
    ACCESS_TOKEN_PREFIX,
    AUTHORIZATION_CODE_PREFIX,
    REFRESH_TOKEN_PREFIX,
    generate_token,
    hash_token,
    matches,
    pkce_challenge_valid,
)
from app.context import AppContext, get_context
from app.database import accounts as accounts_db
from app.database import oauth as oauth_db
from app.database.oauth import OAuthAppRow
from app.errors import bad_request, not_found
from app.models import schemas

router = APIRouter(tags=["oauth"])

WEBSITE_ONLY = ("website",)
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
CODE_CHALLENGE_PATTERN = r"^[A-Za-z0-9_-]{43,128}$"


class ConsentScreen(schemas.Schema):
    """What the consent page shows.

    Attributes:
        client_id: App client id.
        name: App name.
        description: App description.
        redirect_uri: Where the user is sent back to.
        scopes: Requested known scopes.
    """

    client_id: str
    name: str
    description: str | None = None
    redirect_uri: str
    scopes: list[schemas.ScopeInfo]


class AuthorizeBody(schemas.Schema):
    """The user's approval.

    Attributes:
        client_id: App client id.
        redirect_uri: Redirect URI.
        scopes: Approved scopes.
        state: Value echoed back to the app.
        code_challenge: PKCE challenge.
        code_challenge_method: PKCE method, only S256.
    """

    client_id: str
    redirect_uri: str
    scopes: list[OAuthScope] = Field(min_length=1)
    state: str | None = None
    code_challenge: str | None = Field(default=None, pattern=CODE_CHALLENGE_PATTERN)
    code_challenge_method: Literal["S256"] | None = None

    @model_validator(mode="after")
    def default_method(self) -> "AuthorizeBody":
        """Require a challenge with a method and default the method to S256.

        Returns:
            AuthorizeBody: The validated body.
        """
        if self.code_challenge_method and not self.code_challenge:
            raise ValueError("code_challenge_method requires code_challenge")
        if self.code_challenge and not self.code_challenge_method:
            self.code_challenge_method = "S256"
        return self


class AuthorizeResult(schemas.Schema):
    """Where to send the user after approval.

    Attributes:
        code: Authorization code.
        state: Echoed state.
        redirect_uri: Redirect URI with the code and state added.
    """

    code: str
    state: str | None = None
    redirect_uri: str


def oauth_error(
    error: str, description: str, status_code: int = 400, basic: bool = False
) -> JSONResponse:
    """Build an RFC 6749 error response.

    Args:
        error: Error value.
        description: Human-readable description.
        status_code: HTTP status.
        basic: Whether the client used HTTP Basic authentication.

    Returns:
        JSONResponse: The response.
    """
    headers = dict(NO_STORE)
    if status_code == 401 and basic:
        headers["WWW-Authenticate"] = 'Basic realm="oauth"'
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers=headers,
    )


def basic_credentials(authorization: str | None) -> tuple[str | None, str | None]:
    """Read client credentials from an HTTP Basic header.

    Args:
        authorization: Authorization header.

    Returns:
        tuple[str | None, str | None]: Client id and secret, or Nones.
    """
    if not authorization or not authorization.lower().startswith("basic "):
        return None, None
    try:
        client_id, client_secret = base64.b64decode(authorization[6:]).decode().split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None, None
    return client_id, client_secret


def client_authenticated(app: OAuthAppRow, client_secret: str | None) -> bool:
    """Check an app's client secret; public apps rely on PKCE instead.

    Args:
        app: The app.
        client_secret: Secret sent by the client.

    Returns:
        bool: Whether the client is authenticated.
    """
    if app.public:
        return True
    return (
        bool(client_secret)
        and bool(app.client_secret_hash)
        and matches(hash_token(client_secret or ""), app.client_secret_hash or "")
    )


def with_query(uri: str, params: dict[str, str]) -> str:
    """Add query parameters to a URI.

    Args:
        uri: Base URI.
        params: Parameters to add.

    Returns:
        str: The URI.
    """
    parsed = urlparse(uri)
    query = dict(parse_qsl(parsed.query))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))


async def _checked_app(
    context: AppContext, client_id: str, redirect_uri: str, has_challenge: bool
) -> OAuthAppRow:
    """Load an app and validate the redirect URI and PKCE requirement.

    Args:
        context: App context.
        client_id: Client id.
        redirect_uri: Requested redirect URI.
        has_challenge: Whether a PKCE challenge was sent.

    Returns:
        OAuthAppRow: The app.
    """
    async with context.database.connection() as connection:
        app = await oauth_db.get_app(connection, client_id)
    if app is None:
        raise not_found("error.oauth.unknown_client")
    if redirect_uri not in app.redirect_uris:
        raise bad_request("error.oauth.invalid_redirect_uri")
    if app.public and not has_challenge:
        raise bad_request("error.oauth.pkce_required")
    return app


@router.get("/v1/oauth/authorize", response_model=ConsentScreen)
async def consent_screen(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal,
        Depends(auth(account=True, oauth=False, banned=False, session_types=WEBSITE_ONLY)),
    ],
    client_id: str,
    redirect_uri: str,
    scope: str,
    response_type: str = "code",
    code_challenge: Annotated[str | None, Query(pattern=CODE_CHALLENGE_PATTERN)] = None,
) -> ConsentScreen:
    """Describe an authorization request for the consent page.

    Args:
        context: App context.
        principal: The logged-in user.
        client_id: App client id.
        redirect_uri: Redirect URI.
        scope: Space-separated scopes.
        response_type: Must be ``code``.
        code_challenge: PKCE challenge.

    Returns:
        ConsentScreen: What to show.
    """
    if response_type != "code":
        raise bad_request("error.oauth.unsupported_response_type")
    scopes = parse_scopes(scope)
    if not scopes:
        raise bad_request("error.oauth.invalid_scope")
    app = await _checked_app(context, client_id, redirect_uri, code_challenge is not None)
    return ConsentScreen(
        client_id=app.client_id,
        name=app.name,
        description=app.description,
        redirect_uri=redirect_uri,
        scopes=[
            schemas.ScopeInfo(scope=item, description=SCOPE_DESCRIPTIONS[item]) for item in scopes
        ],
    )


@router.post("/v1/oauth/authorize", response_model=AuthorizeResult)
async def authorize(
    body: AuthorizeBody,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal,
        Depends(auth(account=True, oauth=False, banned=False, session_types=WEBSITE_ONLY)),
    ],
) -> AuthorizeResult:
    """Approve an app and issue an authorization code.

    Args:
        body: The approval.
        context: App context.
        principal: The logged-in user.

    Returns:
        AuthorizeResult: Where to send the user.
    """
    account = principal.require_account()
    await _checked_app(context, body.client_id, body.redirect_uri, body.code_challenge is not None)
    code = generate_token(AUTHORIZATION_CODE_PREFIX)
    async with context.database.transaction() as connection:
        await oauth_db.create_authorization_code(
            connection,
            hash_token(code),
            body.client_id,
            account.sonolus_id,
            list(body.scopes),
            body.redirect_uri,
            body.code_challenge,
            context.constants.oauth.authorization_code_seconds,
        )
    params = {"code": code}
    if body.state is not None:
        params["state"] = body.state
    return AuthorizeResult(
        code=code, state=body.state, redirect_uri=with_query(body.redirect_uri, params)
    )


@router.post("/v1/oauth/token")
async def token(
    context: Annotated[AppContext, Depends(get_context)],
    grant_type: Annotated[str, Form()],
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
    code: Annotated[str | None, Form()] = None,
    redirect_uri: Annotated[str | None, Form()] = None,
    code_verifier: Annotated[str | None, Form()] = None,
    refresh_token: Annotated[str | None, Form()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Exchange an authorization code or refresh token for tokens.

    Args:
        context: App context.
        grant_type: ``authorization_code`` or ``refresh_token``.
        client_id: Client id, unless sent with HTTP Basic.
        client_secret: Client secret, unless sent with HTTP Basic.
        code: Authorization code.
        redirect_uri: Redirect URI the code was issued for.
        code_verifier: PKCE verifier.
        refresh_token: Refresh token.
        authorization: Authorization header for HTTP Basic client authentication.

    Returns:
        JSONResponse: The tokens or an RFC 6749 error.
    """
    basic_id, basic_secret = basic_credentials(authorization)
    used_basic = basic_id is not None
    client_id = client_id or basic_id
    client_secret = client_secret or basic_secret
    if not client_id:
        return oauth_error("invalid_client", "Missing client credentials.", 401, used_basic)
    oauth = context.constants.oauth
    async with context.database.transaction() as connection:
        app = await oauth_db.get_app(connection, client_id)
        if app is None or not client_authenticated(app, client_secret):
            return oauth_error("invalid_client", "Invalid client credentials.", 401, used_basic)
        if grant_type == "authorization_code":
            if not code or not redirect_uri:
                return oauth_error("invalid_request", "Missing code or redirect_uri.")
            grant = await oauth_db.consume_authorization_code(connection, hash_token(code))
            if grant is None:
                return oauth_error("invalid_grant", "Invalid or already used code.")
            if grant.expires_at < datetime.now(timezone.utc):
                return oauth_error("invalid_grant", "Expired code.")
            if grant.client_id != client_id:
                return oauth_error("invalid_grant", "Code was issued to another client.")
            if grant.redirect_uri != redirect_uri:
                return oauth_error("invalid_grant", "redirect_uri mismatch.")
            if app.public and not grant.code_challenge:
                return oauth_error("invalid_grant", "PKCE is required for public clients.")
            if not grant.code_challenge and code_verifier:
                return oauth_error("invalid_grant", "code_verifier sent without a code_challenge.")
            if grant.code_challenge and (
                not code_verifier or not pkce_challenge_valid(code_verifier, grant.code_challenge)
            ):
                return oauth_error("invalid_grant", "Invalid code_verifier.")
            user_id, scopes = grant.user_id, grant.scopes
        elif grant_type == "refresh_token":
            if not refresh_token:
                return oauth_error("invalid_request", "Missing refresh_token.")
            refresh_hash = hash_token(refresh_token)
            rotated = await oauth_db.rotate_refresh_token(connection, refresh_hash, client_id)
            if rotated is None:
                reused = await oauth_db.find_reused_refresh_token(
                    connection, refresh_hash, client_id
                )
                if reused is not None:
                    await oauth_db.revoke_authorization(connection, reused.user_id, client_id)
                return oauth_error("invalid_grant", "Invalid or expired refresh_token.")
            user_id, scopes = rotated.user_id, rotated.scopes
        else:
            return oauth_error("unsupported_grant_type", "Unsupported grant type.")
        account = await accounts_db.get_account(connection, user_id)
        if account is None or account.banned:
            return oauth_error("invalid_grant", "The account is banned.")
        access_token = generate_token(ACCESS_TOKEN_PREFIX)
        new_refresh_token = generate_token(REFRESH_TOKEN_PREFIX)
        await oauth_db.create_token(
            connection,
            hash_token(access_token),
            hash_token(new_refresh_token),
            client_id,
            user_id,
            scopes,
            oauth.access_token_seconds,
            oauth.refresh_token_seconds,
        )
    return JSONResponse(
        {
            "access_token": access_token,
            "refresh_token": new_refresh_token,
            "token_type": "Bearer",
            "expires_in": oauth.access_token_seconds,
            "scope": " ".join(scopes),
        },
        headers=NO_STORE,
    )


@router.post("/v1/oauth/revoke")
async def revoke(
    context: Annotated[AppContext, Depends(get_context)],
    token: Annotated[str, Form()],
    client_id: Annotated[str | None, Form()] = None,
    client_secret: Annotated[str | None, Form()] = None,
    token_type_hint: Annotated[str | None, Form()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Revoke an access or refresh token and its pair (RFC 7009).

    Args:
        context: App context.
        token: Token to revoke.
        client_id: Client id, unless sent with HTTP Basic.
        client_secret: Client secret, unless sent with HTTP Basic.
        token_type_hint: Ignored hint.
        authorization: Authorization header for HTTP Basic client authentication.

    Returns:
        JSONResponse: Empty object, or an RFC 6749 error.
    """
    basic_id, basic_secret = basic_credentials(authorization)
    used_basic = basic_id is not None
    client_id = client_id or basic_id
    client_secret = client_secret or basic_secret
    if not client_id:
        return oauth_error("invalid_client", "Missing client credentials.", 401, used_basic)
    async with context.database.connection() as connection:
        app = await oauth_db.get_app(connection, client_id)
        if app is None or not client_authenticated(app, client_secret):
            return oauth_error("invalid_client", "Invalid client credentials.", 401, used_basic)
        await oauth_db.revoke_token(connection, hash_token(token), client_id)
    return JSONResponse({}, headers=NO_STORE)


@router.get("/.well-known/oauth-authorization-server")
async def discovery(
    context: Annotated[AppContext, Depends(get_context)],
) -> JSONResponse:
    """Publish OAuth server metadata (RFC 8414).

    Args:
        context: App context.

    Returns:
        JSONResponse: The metadata.
    """
    base = context.config.server.public_url.rstrip("/")
    website = context.config.server.website_url.rstrip("/")
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{website}/oauth",
            "token_endpoint": f"{base}/v1/oauth/token",
            "revocation_endpoint": f"{base}/v1/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
                "none",
            ],
            "revocation_endpoint_auth_methods_supported": [
                "client_secret_basic",
                "client_secret_post",
                "none",
            ],
            "scopes_supported": list(ALL_SCOPES),
        }
    )
