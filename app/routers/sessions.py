from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from app.auth import sonolus
from app.auth.principal import Principal, auth
from app.auth.tokens import (
    SESSION_TOKEN_PREFIX,
    generate_signed_id,
    generate_token,
    hash_token,
    hmac_hex,
    matches,
)
from app.context import AppContext, get_context
from app.database import accounts as accounts_db
from app.database import connections as connections_db
from app.database import login_requests as login_requests_db
from app.database import sessions as sessions_db
from app.database.core import Connection
from app.database.sessions import SessionRow, SessionType
from app.errors import ApiError, not_found
from app.models import schemas
from app.services import providers
from app.settings import ProviderConfig

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


async def issue_session(
    context: AppContext,
    connection: Connection,
    sonolus_id: str,
    session_type: SessionType,
) -> tuple[str, SessionRow]:
    """Create a session and return its token.

    Args:
        context: App context.
        connection: Database connection inside a transaction.
        sonolus_id: Account id.
        session_type: Session type.

    Returns:
        tuple[str, SessionRow]: The bearer token and the session.
    """
    constants = context.constants.sessions
    lifetime = constants.game_seconds if session_type == "game" else constants.website_seconds
    token = generate_token(SESSION_TOKEN_PREFIX)
    session = await sessions_db.create_session(
        connection,
        sonolus_id,
        session_type,
        hash_token(token),
        lifetime,
        constants.max_per_type,
    )
    return token, session


def poll_secret(context: AppContext, request_id: str) -> str:
    """Derive the secret needed to collect a website login.

    Args:
        context: App context.
        request_id: Login request id.

    Returns:
        str: The poll secret.
    """
    return hmac_hex(context.config.keys.token_secret, "poll:" + request_id)


def provider_config(context: AppContext, provider: providers.Provider) -> ProviderConfig:
    """Return an enabled provider's settings or fail.

    Args:
        context: App context.
        provider: Provider name.

    Returns:
        ProviderConfig: The settings.
    """
    config = getattr(context.config.connections, provider)
    if not config.enabled:
        raise not_found("error.connection.provider_disabled", provider=provider)
    return config


@router.post("/game", status_code=201, response_model=schemas.NewSession)
async def create_game_session(
    request: Request,
    context: Annotated[AppContext, Depends(get_context)],
    signature: Annotated[str | None, Header(alias="Sonolus-Signature")] = None,
) -> schemas.NewSession:
    """Log in from the Sonolus app with a body Sonolus signed.

    Args:
        request: Current request.
        context: App context.
        signature: Sonolus-Signature header.

    Returns:
        schemas.NewSession: The game session.
    """
    body = await request.body()
    sonolus.verify_signature(context.sonolus_key, body, signature)
    payload = sonolus.parse_authenticate(
        body,
        context.config.sonolus.server_url,
        context.constants.sonolus.signature_time_window_seconds,
    )
    profile = payload.userProfile
    try:
        handle = int(profile.handle)
    except ValueError:
        raise ApiError(400, "error.sonolus.payload_invalid") from None
    async with context.database.transaction() as connection:
        await accounts_db.upsert_account(connection, profile.id, handle, profile.name)
        token, session = await issue_session(context, connection, profile.id, "game")
    return schemas.NewSession(session=token, expires_at=session.expires_at)


@router.post("/website/login-requests", status_code=201, response_model=schemas.LoginRequest)
async def create_login_request(
    context: Annotated[AppContext, Depends(get_context)],
) -> schemas.LoginRequest:
    """Start a website login through the Sonolus app.

    Args:
        context: App context.

    Returns:
        schemas.LoginRequest: The login request.
    """
    request_id = generate_signed_id(context.config.keys.token_secret)
    async with context.database.connection() as connection:
        row = await login_requests_db.create_login_request(
            connection, request_id, context.constants.login_requests.link_seconds
        )
    return schemas.LoginRequest(
        id=request_id,
        poll_secret=poll_secret(context, request_id),
        expires_at=row.expires_at,
    )


@router.post("/website/login-requests/{request_id}/approve", status_code=204)
async def approve_login_request(
    request_id: str,
    request: Request,
    context: Annotated[AppContext, Depends(get_context)],
    signature: Annotated[str | None, Header(alias="Sonolus-Signature")] = None,
) -> Response:
    """Approve a website login with a body Sonolus signed.

    Args:
        request_id: Login request id.
        request: Current request.
        context: App context.
        signature: Sonolus-Signature header.

    Returns:
        Response: Empty response.
    """
    body = await request.body()
    sonolus.verify_signature(context.sonolus_key, body, signature)
    payload = sonolus.parse_external(
        body,
        context.config.sonolus.server_url,
        request_id,
        context.constants.sonolus.signature_time_window_seconds,
    )
    profile = payload.userProfile
    try:
        handle = int(profile.handle)
    except ValueError:
        raise ApiError(400, "error.sonolus.payload_invalid") from None
    async with context.database.transaction() as connection:
        pending = await login_requests_db.lock_pending_login_request(connection, request_id)
        if pending is None:
            raise not_found("error.login_request.not_found")
        await accounts_db.upsert_account(connection, profile.id, handle, profile.name)
        token, _ = await issue_session(context, connection, profile.id, "website")
        await login_requests_db.approve_login_request(
            connection,
            request_id,
            token,
            context.constants.login_requests.pickup_seconds,
        )
    return Response(status_code=204)


@router.get("/website/login-requests/{request_id}", response_model=schemas.NewSession)
async def poll_login_request(
    request_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    secret: Annotated[str, Query(alias="poll_secret")],
) -> Response:
    """Collect the session of an approved website login.

    Args:
        request_id: Login request id.
        context: App context.
        secret: Poll secret from the create call.

    Returns:
        Response: 200 with the session once approved, 202 while waiting.
    """
    if not matches(secret, poll_secret(context, request_id)):
        raise not_found("error.login_request.not_found")
    async with context.database.connection() as connection:
        token = await login_requests_db.take_approved_login_request(connection, request_id)
        if token is None:
            if await login_requests_db.get_login_request(connection, request_id) is None:
                raise not_found("error.login_request.not_found")
            return Response(status_code=202)
        resolved = await sessions_db.get_session_by_token_hash(connection, hash_token(token))
    if resolved is None:
        raise not_found("error.login_request.not_found")
    body = schemas.NewSession(session=token, expires_at=resolved.session.expires_at)
    return JSONResponse(body.model_dump(mode="json"))


@router.post("/website/{provider}/start", response_model=schemas.AuthorizationStart)
async def start_provider_sign_in(
    provider: providers.Provider,
    context: Annotated[AppContext, Depends(get_context)],
) -> schemas.AuthorizationStart:
    """Start signing in with a linked Discord or Google account.

    Args:
        provider: Provider name.
        context: App context.

    Returns:
        schemas.AuthorizationStart: Where to send the user.
    """
    config = provider_config(context, provider)
    secret = context.config.keys.token_secret
    state = providers.encode_state(
        secret, provider, "sign_in", None, context.constants.connections.state_seconds
    )
    return schemas.AuthorizationStart(
        url=providers.authorize_url(config, provider, state, secret, providers.state_nonce(state))
    )


@router.post("/website/{provider}", status_code=201, response_model=schemas.NewSession)
async def finish_provider_sign_in(
    provider: providers.Provider,
    body: schemas.ProviderCallback,
    context: Annotated[AppContext, Depends(get_context)],
) -> schemas.NewSession:
    """Finish signing in with a linked Discord or Google account.

    Args:
        provider: Provider name.
        body: Code and state from the provider redirect.
        context: App context.

    Returns:
        schemas.NewSession: The website session.
    """
    config = provider_config(context, provider)
    secret = context.config.keys.token_secret
    state = providers.decode_state(secret, body.state, provider, "sign_in")
    identity = await providers.exchange_code(
        context.http,
        config,
        provider,
        body.code,
        providers.code_verifier(secret, state.nonce),
    )
    async with context.database.transaction() as connection:
        link = await connections_db.find_by_provider_user(connection, provider, identity.user_id)
        if link is None:
            raise not_found("error.connection.not_linked", provider=provider)
        if config.store_tokens:
            await connections_db.save_connection(
                connection,
                link.sonolus_id,
                provider,
                identity.user_id,
                identity.display_name,
                context.cipher.encrypt(identity.access_token, link.sonolus_id),
                (
                    context.cipher.encrypt(identity.refresh_token, link.sonolus_id)
                    if identity.refresh_token
                    else None
                ),
                identity.expires_at,
            )
        token, session = await issue_session(context, connection, link.sonolus_id, "website")
    return schemas.NewSession(session=token, expires_at=session.expires_at)


@router.get("", response_model=list[schemas.SessionInfo])
async def list_sessions(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, oauth=False))],
) -> list[schemas.SessionInfo]:
    """List the caller's active sessions.

    Args:
        context: App context.
        principal: The caller.

    Returns:
        list[schemas.SessionInfo]: The sessions, newest first.
    """
    account = principal.require_account()
    current_id = principal.session.id if principal.session else None
    async with context.database.connection() as connection:
        rows = await sessions_db.list_sessions(connection, account.sonolus_id)
    return [
        schemas.SessionInfo(
            id=row.id,
            type=row.type,
            created_at=row.created_at,
            expires_at=row.expires_at,
            current=row.id == current_id,
        )
        for row in rows
    ]


@router.delete("/current", status_code=204)
async def log_out(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, oauth=False))],
) -> Response:
    """Log out the session making the request.

    Args:
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    account = principal.require_account()
    if principal.session is not None:
        async with context.database.connection() as connection:
            await sessions_db.delete_session(connection, principal.session.id, account.sonolus_id)
    return Response(status_code=204)


@router.delete("/{session_id}", status_code=204)
async def revoke_session(
    session_id: UUID,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, oauth=False))],
) -> Response:
    """Revoke one of the caller's sessions.

    Args:
        session_id: Session id.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        if not await sessions_db.delete_session(connection, session_id, account.sonolus_id):
            raise not_found("error.session.not_found")
    return Response(status_code=204)


@router.delete("", status_code=204)
async def log_out_everywhere(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, oauth=False))],
) -> Response:
    """Revoke every session of the caller.

    Args:
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        await sessions_db.delete_all_sessions(connection, account.sonolus_id)
    return Response(status_code=204)
