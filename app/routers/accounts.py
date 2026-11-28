from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import Field

from app.auth.catalog import StaffPermission
from app.badges import BADGES, GrantableBadge
from app.auth.principal import Principal, auth
from app.context import AppContext, get_context
from app.database import accounts as accounts_db
from app.database import badges as badges_db
from app.database import charts as charts_db
from app.database import notifications as notifications_db
from app.database import oauth as oauth_db
from app.database import permissions as permissions_db
from app.database import sessions as sessions_db
from app.database import staff_actions as staff_actions_db
from app.database.accounts import AccountRow
from app.database.charts import ChartFilters
from app.database.core import Connection
from app.errors import bad_request, not_found
from app.models import present, schemas
from app.services import account_collaborations, account_tags, purge, staff_log
from app.services.chart_access import check_chart_id

router = APIRouter(prefix="/v1", tags=["accounts"])


class BanRequest(schemas.Schema):
    """Ban options.

    Attributes:
        delete: Also schedule the account and its content for deletion; False keeps a deletion
            that is already pending.
    """

    delete: bool = True


class BadgeGrant(schemas.Schema):
    """Badge level to grant.

    Attributes:
        level: Badge level, 1 by default.
    """

    level: int = 1


class SendNotification(schemas.Schema):
    """A notification to send.

    Attributes:
        account_id: Recipient account; ignored when chart_id is given.
        chart_id: Send to this chart's owner.
        title: Title.
        content: Body text.
    """

    account_id: str | None = None
    chart_id: str | None = None
    title: str = Field(min_length=1)
    content: str | None = None


async def _target_account(connection: Connection, sonolus_id: str) -> AccountRow:
    """Load an account acted on by staff, or fail.

    Args:
        connection: Database connection.
        sonolus_id: Account id.

    Returns:
        AccountRow: The account.
    """
    account = await accounts_db.get_account(connection, sonolus_id)
    if account is None:
        raise not_found("error.account.not_found")
    return account


async def _check_staff_target(
    connection: Connection, principal: Principal, target: AccountRow
) -> None:
    """Reject acting on yourself or on someone the caller doesn't outrank.

    Args:
        connection: Database connection.
        principal: The caller.
        target: Target account.
    """
    if principal.sonolus_id == target.sonolus_id:
        raise bad_request("error.staff.self_target")
    principal.require_outranks(await permissions_db.get_permissions(connection, target.sonolus_id))


@router.get("/accounts/by-handle/{handle}", response_model=schemas.PublicAccount)
async def get_account_by_handle(
    handle: int,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
) -> schemas.PublicAccount:
    """Look up an account by Sonolus handle.

    Args:
        handle: Sonolus handle.
        context: App context.
        principal: The caller.

    Returns:
        schemas.PublicAccount: The account.
    """
    async with context.database.connection() as connection:
        account = await accounts_db.get_account_by_handle(connection, handle)
        if account is None:
            raise not_found("error.account.not_found")
        permissions = await permissions_db.get_permissions(connection, account.sonolus_id)
        badges = await account_tags.load_badges(
            connection, account.sonolus_id, permissions, context.storage
        )
    return present.public_account(account, permissions, badges, context.storage)


@router.get("/accounts/{account_id}", response_model=schemas.Profile)
async def get_profile(
    account_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
) -> schemas.Profile:
    """Return a public profile with its most liked owned and credited charts.

    Args:
        account_id: Account id.
        context: App context.
        principal: The caller.

    Returns:
        schemas.Profile: The profile.
    """
    async with context.database.connection() as connection:
        account = await accounts_db.get_account(connection, account_id)
        if account is None:
            raise not_found("error.account.not_found")
        permissions = await permissions_db.get_permissions(connection, account_id)
        badges = await account_tags.load_badges(
            connection, account_id, permissions, context.storage
        )
        rows, _ = await charts_db.list_charts(
            connection,
            ChartFilters(owned_by=account_id, sort_by="likes"),
            0,
            context.constants.charts.profile_top_charts,
        )
        credited, _ = await charts_db.list_charts(
            connection,
            ChartFilters(credited_account=account_id, sort_by="likes"),
            0,
            context.constants.charts.profile_top_charts,
        )
    return schemas.Profile(
        account=present.public_account(account, permissions, badges, context.storage),
        charts=[present.chart(row, context.storage) for row in rows],
        collaborations=[present.chart(row, context.storage) for row in credited],
    )


@router.get("/accounts/{account_id}/stats", response_model=schemas.AccountStats)
async def get_stats(
    account_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
) -> schemas.AccountStats:
    """Return an account's activity statistics.

    Args:
        account_id: Account id.
        context: App context.
        principal: The caller.

    Returns:
        schemas.AccountStats: The statistics.
    """
    async with context.database.connection() as connection:
        stats = await accounts_db.get_stats(connection, account_id)
    if stats is None:
        raise not_found("error.account.not_found")
    return schemas.AccountStats(
        liked_charts=stats.liked_charts_count,
        comments=stats.comments_count,
        charts_published=stats.charts_published,
        likes_received=stats.likes_received,
        comments_received=stats.comments_received,
    )


@router.put("/accounts/{account_id}/ban", status_code=204)
async def ban_account(
    account_id: str,
    body: BanRequest,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False, banned=False))],
) -> Response:
    """Ban an account, ending its sessions and app access.

    Args:
        account_id: Account id.
        body: Ban options.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    principal.require_permission("account.ban")
    async with context.database.transaction() as connection:
        target = await _target_account(connection, account_id)
        await _check_staff_target(connection, principal, target)
        previous = await accounts_db.set_banned(connection, account_id, True)
        if body.delete:
            await accounts_db.set_pending_deletion(connection, account_id, True)
        await sessions_db.delete_all_sessions(connection, account_id)
        await oauth_db.revoke_all_for_account(connection, account_id)
        await account_collaborations.remove_all(connection, principal, account_id)
        await staff_log.record(
            context,
            connection,
            principal,
            "ban",
            "account",
            account_id,
            str(previous),
            "True",
        )
    return Response(status_code=204)


@router.delete("/accounts/{account_id}/ban", status_code=204)
async def unban_account(
    account_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False, banned=False))],
) -> Response:
    """Unban an account, cancel its pending deletion and restore its collaborations.

    Args:
        account_id: Account id.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    principal.require_permission("account.ban")
    async with context.database.transaction() as connection:
        target = await _target_account(connection, account_id)
        await _check_staff_target(connection, principal, target)
        previous = await accounts_db.set_banned(connection, account_id, False)
        await accounts_db.set_pending_deletion(connection, account_id, False)
        await account_collaborations.restore_all(connection, account_id)
        await staff_log.record(
            context,
            connection,
            principal,
            "ban",
            "account",
            account_id,
            str(previous),
            "False",
        )
    return Response(status_code=204)


@router.delete("/accounts/{account_id}", status_code=204)
async def delete_account(
    account_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False, banned=False))],
) -> Response:
    """Delete an account and everything it stored, immediately.

    Args:
        account_id: Account id.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    principal.require_permission("account.delete")
    async with context.database.connection() as connection:
        target = await _target_account(connection, account_id)
        await _check_staff_target(connection, principal, target)
    if not await purge.purge_account(context, account_id):
        raise not_found("error.account.not_found")
    async with context.database.connection() as connection:
        await staff_log.record(
            context,
            connection,
            principal,
            "account_delete",
            "account",
            account_id,
            f"{target.sonolus_username}#{target.sonolus_handle}",
            None,
        )
    return Response(status_code=204)


@router.get("/accounts/{account_id}/permissions", response_model=list[schemas.PermissionGrant])
async def list_permissions(
    account_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False))],
) -> list[schemas.PermissionGrant]:
    """List an account's staff permissions.

    Args:
        account_id: Account id.
        context: App context.
        principal: The caller.

    Returns:
        list[schemas.PermissionGrant]: The grants.
    """
    principal.require_permission("staff.manage")
    async with context.database.connection() as connection:
        await _target_account(connection, account_id)
        grants = await permissions_db.list_grants(connection, account_id)
    return [
        schemas.PermissionGrant(
            permission=grant.permission,
            granted_by=grant.granted_by,
            granted_at=grant.granted_at,
        )
        for grant in grants
    ]


@router.put("/accounts/{account_id}/permissions/{permission}", status_code=204)
async def grant_permission(
    account_id: str,
    permission: StaffPermission,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False, banned=False))],
) -> Response:
    """Grant a staff permission the caller holds.

    Args:
        account_id: Account id.
        permission: Permission name.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    principal.require_permission("staff.manage")
    principal.require_permission(permission)
    async with context.database.transaction() as connection:
        target = await _target_account(connection, account_id)
        await _check_staff_target(connection, principal, target)
        if await permissions_db.grant_permission(
            connection, account_id, permission, principal.sonolus_id
        ):
            await staff_log.record(
                context,
                connection,
                principal,
                "permission_grant",
                "account",
                account_id,
                None,
                permission,
            )
    return Response(status_code=204)


@router.delete("/accounts/{account_id}/permissions/{permission}", status_code=204)
async def revoke_permission(
    account_id: str,
    permission: StaffPermission,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False, banned=False))],
) -> Response:
    """Revoke a staff permission.

    Args:
        account_id: Account id.
        permission: Permission name.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    principal.require_permission("staff.manage")
    principal.require_permission(permission)
    async with context.database.transaction() as connection:
        target = await _target_account(connection, account_id)
        await _check_staff_target(connection, principal, target)
        if await permissions_db.revoke_permission(connection, account_id, permission):
            await staff_log.record(
                context,
                connection,
                principal,
                "permission_revoke",
                "account",
                account_id,
                permission,
                None,
            )
    return Response(status_code=204)


@router.put("/accounts/{account_id}/badges/{badge}", status_code=204)
async def grant_badge(
    account_id: str,
    badge: GrantableBadge,
    body: BadgeGrant,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False, banned=False))],
) -> Response:
    """Give an account a badge, or change its level.

    Args:
        account_id: Account id.
        badge: Badge name.
        body: Badge level.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    principal.require_permission("staff.manage")
    max_level = BADGES[badge].max_level
    if not 1 <= body.level <= max_level:
        raise bad_request("error.badge.level_out_of_range", min=1, max=max_level)
    async with context.database.transaction() as connection:
        target = await _target_account(connection, account_id)
        await _check_staff_target(connection, principal, target)
        previous = await badges_db.set_badge(
            connection, account_id, badge, body.level, principal.sonolus_id
        )
        if previous != body.level:
            await staff_log.record(
                context,
                connection,
                principal,
                "badge_grant",
                "account",
                account_id,
                f"{badge}:{previous}" if previous is not None else None,
                f"{badge}:{body.level}",
            )
    return Response(status_code=204)


@router.delete("/accounts/{account_id}/badges/{badge}", status_code=204)
async def remove_badge(
    account_id: str,
    badge: GrantableBadge,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False, banned=False))],
) -> Response:
    """Take a badge away from an account.

    Args:
        account_id: Account id.
        badge: Badge name.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    principal.require_permission("staff.manage")
    async with context.database.transaction() as connection:
        target = await _target_account(connection, account_id)
        await _check_staff_target(connection, principal, target)
        previous = await badges_db.remove_badge(connection, account_id, badge)
        if previous is not None:
            await staff_log.record(
                context,
                connection,
                principal,
                "badge_revoke",
                "account",
                account_id,
                f"{badge}:{previous}",
                None,
            )
    return Response(status_code=204)


@router.post("/notifications", status_code=201, response_model=schemas.Notification)
async def send_notification(
    body: SendNotification,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False, banned=False))],
) -> schemas.Notification:
    """Send a notification to an account or a chart's owner.

    Args:
        body: The notification.
        context: App context.
        principal: The caller.

    Returns:
        schemas.Notification: The sent notification.
    """
    principal.require_permission("notification.send")
    limits = context.constants.text.max_lengths
    if len(body.title) > limits["notification_title"]:
        raise bad_request("error.text.too_long", field="title", limit=limits["notification_title"])
    if body.content is not None and len(body.content) > limits["notification_content"]:
        raise bad_request(
            "error.text.too_long", field="content", limit=limits["notification_content"]
        )
    async with context.database.connection() as connection:
        recipient = body.account_id
        if body.chart_id is not None:
            check_chart_id(body.chart_id)
            chart = await charts_db.get_chart(connection, body.chart_id)
            if chart is None:
                raise not_found("error.chart.not_found")
            recipient = chart.author
        if recipient is None:
            raise bad_request("error.notification.no_recipient")
        row = await notifications_db.create_notification(
            connection, recipient, body.title, body.content
        )
    if row is None:
        raise not_found("error.account.not_found")
    return present.notification(row, include_content=True)


@router.get("/staff-actions", response_model=schemas.Page[schemas.StaffAction])
async def list_staff_actions(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False))],
    page: Annotated[int, Query(ge=0, le=1000000)] = 0,
    actor_id: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> schemas.Page[schemas.StaffAction]:
    """List logged staff actions, newest first.

    Args:
        context: App context.
        principal: The caller.
        page: Zero-based page.
        actor_id: Only actions by this account.
        action: Only this action name.
        target_type: Only this kind of target.
        target_id: Only this target.
        since: Only actions at or after this time.
        until: Only actions before this time.

    Returns:
        schemas.Page[schemas.StaffAction]: The page.
    """
    principal.require_permission("staff.manage")
    page_size = context.constants.staff_actions.page_size
    async with context.database.connection() as connection:
        rows, total = await staff_actions_db.list_actions(
            connection,
            page,
            page_size,
            actor_id,
            action,
            target_type,
            target_id,
            since,
            until,
        )
        actors = await accounts_db.get_accounts(
            connection, [row.actor_id for row in rows if row.actor_id]
        )
    return schemas.Page[schemas.StaffAction](
        items=[
            schemas.StaffAction(
                id=row.id,
                actor=(
                    present.account_summary(actors[row.actor_id])
                    if row.actor_id in actors
                    else None
                ),
                via_admin_key=row.via_admin_key,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                previous_value=row.previous_value,
                new_value=row.new_value,
                created_at=row.created_at,
            )
            for row in rows
        ],
        page=page,
        page_count=present.page_count(total, page_size),
    )
