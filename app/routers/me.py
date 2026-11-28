from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Path, Query, Response, UploadFile
from pydantic import Field

from app.auth.catalog import SCOPE_DESCRIPTIONS
from app.auth.principal import Principal, auth
from app.context import AppContext, get_context
from app.database import account_settings as settings_db
from app.database import accounts as accounts_db
from app.database import collaborators as collaborators_db
from app.database import connections as connections_db
from app.database import notifications as notifications_db
from app.database import oauth as oauth_db
from app.database import permissions as permissions_db
from app.errors import bad_request, conflict, not_found
from app.models import present, schemas
from app.services import account_tags, chart_files, providers
from app.services.uploads import read_limited
from app.settings import ProviderConfig

router = APIRouter(prefix="/v1/me", tags=["me"])

WEBSITE_ONLY = ("website",)


class UpdateMe(schemas.Schema):
    """Profile fields to change.

    Attributes:
        description: New description, or None to clear it.
    """

    description: str | None = Field(default=None)


class SettingsChange(schemas.Schema):
    """Settings to change; only fields present are changed.

    Attributes:
        collaboration_invites: Whether others may invite the account to collaborate on charts.
    """

    collaboration_invites: bool | None = None


class ReadState(schemas.Schema):
    """New read state of a notification.

    Attributes:
        is_read: Whether the notification is read.
    """

    is_read: bool


def enabled_provider(context: AppContext, provider: providers.Provider) -> ProviderConfig:
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


@router.get("", response_model=schemas.Me)
async def get_me(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="user:read"))],
) -> schemas.Me:
    """Return the caller's account.

    Args:
        context: App context.
        principal: The caller.

    Returns:
        schemas.Me: The account.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        unread = await notifications_db.unread_count(connection, account.sonolus_id)
        permissions = await permissions_db.get_permissions(connection, account.sonolus_id)
        badges = await account_tags.load_badges(
            connection, account.sonolus_id, permissions, context.storage
        )
    return schemas.Me(
        account=present.public_account(account, permissions, badges, context.storage),
        unread_notifications=unread,
        upload_cooldown_until=account.chart_upload_cooldown,
    )


@router.patch("", status_code=204)
async def update_me(
    body: UpdateMe,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="user:edit", banned=False))],
) -> Response:
    """Change the caller's profile description.

    Args:
        body: Fields to change.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    account = principal.require_account()
    if "description" in body.model_fields_set:
        limit = context.constants.text.max_lengths["account_description"]
        if body.description is not None and len(body.description) > limit:
            raise bad_request("error.text.too_long", field="description", limit=limit)
        async with context.database.connection() as connection:
            await accounts_db.update_description(connection, account.sonolus_id, body.description)
    return Response(status_code=204)


@router.get("/settings", response_model=schemas.AccountSettings)
async def get_settings(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="user:read"))],
) -> schemas.AccountSettings:
    """Return the caller's settings.

    Args:
        context: App context.
        principal: The caller.

    Returns:
        schemas.AccountSettings: The settings.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        settings = await settings_db.get_settings(connection, account.sonolus_id)
    return schemas.AccountSettings(**settings.model_dump())


@router.patch("/settings", response_model=schemas.AccountSettings)
async def update_settings(
    body: SettingsChange,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="user:edit", banned=False))],
) -> schemas.AccountSettings:
    """Change some of the caller's settings.

    Args:
        body: Settings to change.
        context: App context.
        principal: The caller.

    Returns:
        schemas.AccountSettings: The settings after the change.
    """
    account = principal.require_account()
    changes = {
        field: getattr(body, field)
        for field in body.model_fields_set
        if getattr(body, field) is not None
    }
    async with context.database.connection() as connection:
        if changes:
            settings = await settings_db.update_settings(connection, account.sonolus_id, changes)
        else:
            settings = await settings_db.get_settings(connection, account.sonolus_id)
    return schemas.AccountSettings(**settings.model_dump())


async def _replace_image(
    context: AppContext, principal: Principal, upload: UploadFile, kind: str
) -> schemas.ImageRef:
    """Replace the caller's avatar or banner.

    Args:
        context: App context.
        principal: The caller.
        upload: Uploaded image.
        kind: ``profile`` or ``banner``.

    Returns:
        schemas.ImageRef: The new image.
    """
    account = principal.require_account()
    limit_key = "avatar" if kind == "profile" else "banner"
    data = await read_limited(upload, context.constants.files.max_sizes[limit_key], "file")
    images = context.constants.images
    size = images.avatar_size if kind == "profile" else images.banner_size
    processed = await context.run_blocking(
        chart_files.process_profile_image, data, size, images.max_pixels
    )
    prefix = f"{account.sonolus_id}/{kind}/"
    old_keys = [
        key
        for key in await context.storage.list_keys(prefix)
        if not key.startswith(prefix + processed.file_hash)
    ]
    await context.storage.put_many(
        [
            (prefix + processed.file_hash, processed.png, "image/png"),
            (prefix + processed.file_hash + "_webp", processed.webp, "image/webp"),
        ]
    )
    async with context.database.connection() as connection:
        if kind == "profile":
            await accounts_db.update_profile_hash(
                connection, account.sonolus_id, processed.file_hash
            )
        else:
            await accounts_db.update_banner_hash(
                connection, account.sonolus_id, processed.file_hash
            )
    await context.storage.discard_keys(old_keys)
    key = prefix + processed.file_hash
    return schemas.ImageRef(
        hash=processed.file_hash,
        url=context.storage.public_url(key),
        webp_url=context.storage.public_url(key + "_webp"),
    )


async def _remove_image(context: AppContext, principal: Principal, kind: str) -> None:
    """Remove the caller's avatar or banner.

    Args:
        context: App context.
        principal: The caller.
        kind: ``profile`` or ``banner``.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        if kind == "profile":
            await accounts_db.update_profile_hash(connection, account.sonolus_id, None)
        else:
            await accounts_db.update_banner_hash(connection, account.sonolus_id, None)
    await context.storage.discard_prefix(f"{account.sonolus_id}/{kind}/")


@router.put("/avatar", response_model=schemas.ImageRef)
async def put_avatar(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="user:edit", banned=False))],
    file: Annotated[UploadFile, File()],
) -> schemas.ImageRef:
    """Replace the caller's avatar.

    Args:
        context: App context.
        principal: The caller.
        file: Uploaded image.

    Returns:
        schemas.ImageRef: The new avatar.
    """
    return await _replace_image(context, principal, file, "profile")


@router.delete("/avatar", status_code=204)
async def delete_avatar(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="user:edit", banned=False))],
) -> Response:
    """Remove the caller's avatar.

    Args:
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    await _remove_image(context, principal, "profile")
    return Response(status_code=204)


@router.put("/banner", response_model=schemas.ImageRef)
async def put_banner(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="user:edit", banned=False))],
    file: Annotated[UploadFile, File()],
) -> schemas.ImageRef:
    """Replace the caller's banner.

    Args:
        context: App context.
        principal: The caller.
        file: Uploaded image.

    Returns:
        schemas.ImageRef: The new banner.
    """
    return await _replace_image(context, principal, file, "banner")


@router.delete("/banner", status_code=204)
async def delete_banner(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="user:edit", banned=False))],
) -> Response:
    """Remove the caller's banner.

    Args:
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    await _remove_image(context, principal, "banner")
    return Response(status_code=204)


@router.get("/notifications", response_model=schemas.NotificationPage)
async def list_notifications(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="notifications:read"))],
    page: Annotated[int, Query(ge=0, le=1000000)] = 0,
    only_unread: bool = False,
    offset: Annotated[int | None, Query(ge=0, le=100000000)] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> schemas.NotificationPage:
    """List the caller's notifications, newest first.

    Args:
        context: App context.
        principal: The caller.
        page: Zero-based page, used when offset is absent.
        only_unread: Only unread notifications.
        offset: Notifications to skip; replaces page.
        limit: Notifications returned, and the page size of page and page_count.

    Returns:
        schemas.NotificationPage: The page.
    """
    account = principal.require_account()
    page_size = limit if limit is not None else context.constants.notifications.page_size
    skip = offset if offset is not None else page * page_size
    async with (
        context.database.connection() as connection,
        connection.transaction(isolation="repeatable_read", readonly=True),
    ):
        rows, total = await notifications_db.list_notifications(
            connection, account.sonolus_id, skip, page_size, only_unread
        )
        unread = await notifications_db.unread_count(connection, account.sonolus_id)
    return schemas.NotificationPage(
        items=[present.notification(row, include_content=False) for row in rows],
        page=skip // page_size,
        page_count=present.page_count(total, page_size),
        total=total,
        unread_count=unread,
    )


@router.get("/notifications/{notification_id}", response_model=schemas.Notification)
async def read_notification(
    notification_id: Annotated[int, Path(ge=1, le=2147483647)],
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="notifications:read"))],
) -> schemas.Notification:
    """Open a notification, marking it read when the caller may change notifications.

    Args:
        notification_id: Notification id.
        context: App context.
        principal: The caller.

    Returns:
        schemas.Notification: The notification.
    """
    account = principal.require_account()
    fetch = (
        notifications_db.read_notification
        if principal.has_scope("notifications:write")
        else notifications_db.get_notification
    )
    async with context.database.connection() as connection:
        row = await fetch(connection, notification_id, account.sonolus_id)
    if row is None:
        raise not_found("error.notification.not_found")
    return present.notification(row, include_content=True)


@router.patch("/notifications/{notification_id}", response_model=schemas.Notification)
async def set_notification_read(
    notification_id: Annotated[int, Path(ge=1, le=2147483647)],
    body: ReadState,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="notifications:write"))],
) -> schemas.Notification:
    """Mark a notification read or unread.

    Args:
        notification_id: Notification id.
        body: New read state.
        context: App context.
        principal: The caller.

    Returns:
        schemas.Notification: The notification.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        row = await notifications_db.set_read(
            connection, notification_id, account.sonolus_id, body.is_read
        )
    if row is None:
        raise not_found("error.notification.not_found")
    return present.notification(row, include_content=True)


@router.delete("/notifications/{notification_id}", status_code=204)
async def delete_notification(
    notification_id: Annotated[int, Path(ge=1, le=2147483647)],
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="notifications:write"))],
) -> Response:
    """Delete one of the caller's notifications.

    Args:
        notification_id: Notification id.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        if not await notifications_db.delete_notification(
            connection, notification_id, account.sonolus_id
        ):
            raise not_found("error.notification.not_found")
    return Response(status_code=204)


@router.get("/connections", response_model=list[schemas.ConnectionInfo])
async def list_connections(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal, Depends(auth(account=True, oauth=False, session_types=WEBSITE_ONLY))
    ],
) -> list[schemas.ConnectionInfo]:
    """List the caller's linked sign-in providers.

    Args:
        context: App context.
        principal: The caller.

    Returns:
        list[schemas.ConnectionInfo]: The links.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        rows = await connections_db.list_connections(connection, account.sonolus_id)
    return [
        schemas.ConnectionInfo(
            provider=row.provider,
            provider_user_id=row.provider_user_id,
            display_name=row.provider_display_name,
            linked_at=row.linked_at,
        )
        for row in rows
    ]


@router.post("/connections/{provider}/start", response_model=schemas.AuthorizationStart)
async def start_link(
    provider: providers.Provider,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal, Depends(auth(account=True, oauth=False, session_types=WEBSITE_ONLY))
    ],
) -> schemas.AuthorizationStart:
    """Start linking a Discord or Google account.

    Args:
        provider: Provider name.
        context: App context.
        principal: The caller.

    Returns:
        schemas.AuthorizationStart: Where to send the user.
    """
    account = principal.require_account()
    config = enabled_provider(context, provider)
    secret = context.config.keys.token_secret
    state = providers.encode_state(
        secret,
        provider,
        "link",
        account.sonolus_id,
        context.constants.connections.state_seconds,
    )
    return schemas.AuthorizationStart(
        url=providers.authorize_url(config, provider, state, secret, providers.state_nonce(state))
    )


@router.post("/connections/{provider}", response_model=schemas.ConnectionInfo)
async def finish_link(
    provider: providers.Provider,
    body: schemas.ProviderCallback,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal, Depends(auth(account=True, oauth=False, session_types=WEBSITE_ONLY))
    ],
) -> schemas.ConnectionInfo:
    """Finish linking a Discord or Google account.

    Args:
        provider: Provider name.
        body: Code and state from the provider redirect.
        context: App context.
        principal: The caller.

    Returns:
        schemas.ConnectionInfo: The new link.
    """
    account = principal.require_account()
    config = enabled_provider(context, provider)
    secret = context.config.keys.token_secret
    state = providers.decode_state(secret, body.state, provider, "link")
    if state.sonolus_id != account.sonolus_id:
        raise bad_request("error.connection.invalid_state")
    identity = await providers.exchange_code(
        context.http,
        config,
        provider,
        body.code,
        providers.code_verifier(secret, state.nonce),
    )
    async with context.database.transaction() as connection:
        existing = await connections_db.find_by_provider_user(
            connection, provider, identity.user_id
        )
        if existing is not None and existing.sonolus_id != account.sonolus_id:
            raise conflict("error.connection.already_linked", provider=provider)
        encrypted_access = encrypted_refresh = None
        if config.store_tokens:
            encrypted_access = context.cipher.encrypt(identity.access_token, account.sonolus_id)
            if identity.refresh_token:
                encrypted_refresh = context.cipher.encrypt(
                    identity.refresh_token, account.sonolus_id
                )
        await connections_db.save_connection(
            connection,
            account.sonolus_id,
            provider,
            identity.user_id,
            identity.display_name,
            encrypted_access,
            encrypted_refresh,
            identity.expires_at if config.store_tokens else None,
        )
        saved = await connections_db.get_connection(connection, account.sonolus_id, provider)
    assert saved is not None
    return schemas.ConnectionInfo(
        provider=saved.provider,
        provider_user_id=saved.provider_user_id,
        display_name=saved.provider_display_name,
        linked_at=saved.linked_at,
    )


@router.delete("/connections/{provider}", status_code=204)
async def unlink(
    provider: providers.Provider,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal, Depends(auth(account=True, oauth=False, session_types=WEBSITE_ONLY))
    ],
) -> Response:
    """Unlink a Discord or Google account.

    Args:
        provider: Provider name.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        if not await connections_db.delete_connection(connection, account.sonolus_id, provider):
            raise not_found("error.connection.not_linked", provider=provider)
    return Response(status_code=204)


@router.get("/oauth-authorizations", response_model=list[schemas.OAuthAuthorization])
async def list_authorizations(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal, Depends(auth(account=True, oauth=False, session_types=WEBSITE_ONLY))
    ],
) -> list[schemas.OAuthAuthorization]:
    """List the third-party apps the caller authorized.

    Args:
        context: App context.
        principal: The caller.

    Returns:
        list[schemas.OAuthAuthorization]: One entry per app.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        rows = await oauth_db.list_authorizations(connection, account.sonolus_id)
    return [
        schemas.OAuthAuthorization(
            client_id=row.client_id,
            name=row.name,
            description=row.description,
            scopes=[
                schemas.ScopeInfo(scope=scope, description=SCOPE_DESCRIPTIONS.get(scope, scope))
                for scope in sorted(row.scopes)
            ],
            authorized_at=row.authorized_at,
            last_used_at=row.last_used_at,
        )
        for row in rows
    ]


@router.delete("/oauth-authorizations/{client_id}", status_code=204)
async def revoke_authorization(
    client_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal, Depends(auth(account=True, oauth=False, session_types=WEBSITE_ONLY))
    ],
) -> Response:
    """Revoke a third-party app's access.

    Args:
        client_id: App client id.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    account = principal.require_account()
    async with context.database.transaction() as connection:
        if not await oauth_db.revoke_authorization(connection, account.sonolus_id, client_id):
            raise not_found("error.oauth.not_authorized")
    return Response(status_code=204)


@router.get("/collaboration-invites", response_model=list[schemas.CollaboratorInvite])
async def list_invites(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="chart:collaborate"))],
) -> list[schemas.CollaboratorInvite]:
    """List collaboration invites the caller received.

    Args:
        context: App context.
        principal: The caller.

    Returns:
        list[schemas.CollaboratorInvite]: The invites.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        rows = await collaborators_db.list_invites_for_account(connection, account.sonolus_id)
    return [
        schemas.CollaboratorInvite(
            id=row.id,
            chart_id=row.chart_id,
            chart_title=row.chart_title,
            account_id=row.sonolus_id,
            permissions=row.permissions,
            invited_by=row.invited_by,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.post("/collaboration-invites/{invite_id}/accept", status_code=204)
async def accept_invite(
    invite_id: UUID,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal, Depends(auth(account=True, scope="chart:collaborate", banned=False))
    ],
) -> Response:
    """Accept a collaboration invite.

    Args:
        invite_id: Invite id.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    account = principal.require_account()
    async with context.database.transaction() as connection:
        invite = await collaborators_db.take_invite(connection, invite_id, account.sonolus_id)
        if invite is None:
            raise not_found("error.collaboration.invite_not_found")
        valid = await collaborators_db.invite_still_valid(connection, invite)
        if valid:
            await collaborators_db.add_collaborator(
                connection,
                invite.chart_id,
                account.sonolus_id,
                invite.permissions,
                invite.invited_by,
            )
    if not valid:
        raise not_found("error.collaboration.invite_not_found")
    return Response(status_code=204)


@router.post("/collaboration-invites/{invite_id}/decline", status_code=204)
async def decline_invite(
    invite_id: UUID,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="chart:collaborate"))],
) -> Response:
    """Decline a collaboration invite.

    Args:
        invite_id: Invite id.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        if await collaborators_db.take_invite(connection, invite_id, account.sonolus_id) is None:
            raise not_found("error.collaboration.invite_not_found")
    return Response(status_code=204)
