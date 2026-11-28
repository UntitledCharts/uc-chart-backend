import json
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import Field

from app.auth.catalog import ChartPermission, with_read
from app.auth.principal import Principal, auth
from app.context import AppContext, get_context
from app.database import account_settings as settings_db
from app.database import accounts as accounts_db
from app.database import collaborators as collaborators_db
from app.database import comments as comments_db
from app.database import leaderboards as leaderboards_db
from app.database import notifications as notifications_db
from app.database import permissions as permissions_db
from app.database.leaderboards import LeaderboardType
from app.errors import bad_request, conflict, forbidden, not_found
from app.models import present, schemas
from app.services import account_tags, staff_log
from app.services.chart_access import ChartAccess, load_chart

router = APIRouter(prefix="/v1/charts/{chart_id}", tags=["chart social"])


class CommentBody(schemas.Schema):
    """A new comment.

    Attributes:
        content: Comment text.
    """

    content: str = Field(min_length=1)


class InviteBody(schemas.Schema):
    """A collaboration invite.

    Attributes:
        account_id: Account to invite.
        permissions: Chart permissions offered.
    """

    account_id: str
    permissions: list[ChartPermission]


class CollaboratorPermissions(schemas.Schema):
    """New collaborator permissions.

    Attributes:
        permissions: Chart permissions.
    """

    permissions: list[ChartPermission]


@router.get("/comments", response_model=schemas.CommentPage)
async def list_comments(
    chart_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
    page: Annotated[int, Query(ge=0, le=1000000)] = 0,
) -> schemas.CommentPage:
    """List a chart's comments, newest first.

    Args:
        chart_id: Chart id.
        context: App context.
        principal: The caller.
        page: Zero-based page.

    Returns:
        schemas.CommentPage: The page.
    """
    page_size = context.constants.comments.page_size
    can_moderate = principal.has_permission("comment.delete")
    async with context.database.connection() as connection:
        await load_chart(connection, principal, chart_id)
        rows, total = await comments_db.list_comments(connection, chart_id, page, page_size)
        commenters = [row.commenter for row in rows if row.commenter]
        accounts = await accounts_db.get_accounts(connection, commenters)
        tags = await account_tags.load(connection, commenters, context.storage)
    return schemas.CommentPage(
        items=[
            present.comment(
                row, principal.sonolus_id, can_moderate, accounts, tags, context.storage
            )
            for row in rows
        ],
        page=page,
        page_count=present.page_count(total, page_size),
        can_moderate=can_moderate,
    )


@router.post("/comments", status_code=201, response_model=schemas.Comment)
async def post_comment(
    chart_id: str,
    body: CommentBody,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal, Depends(auth(account=True, scope="comment:write", banned=False))
    ],
) -> schemas.Comment:
    """Post a comment on a chart.

    Args:
        chart_id: Chart id.
        body: The comment.
        context: App context.
        principal: The caller.

    Returns:
        schemas.Comment: The comment.
    """
    account = principal.require_account()
    limit = context.constants.text.max_lengths["comment"]
    if len(body.content) > limit:
        raise bad_request("error.text.too_long", field="content", limit=limit)
    async with context.database.connection() as connection:
        access = await load_chart(connection, principal, chart_id)
        row = await comments_db.create_comment(
            connection, chart_id, account.sonolus_id, body.content
        )
        tags = await account_tags.load(connection, [account.sonolus_id], context.storage)
    if row is None:
        raise not_found("error.chart.not_found")
    context.discord.new_comment(
        present.discord_chart(access.chart, context.storage),
        f"{account.sonolus_username}#{account.sonolus_handle}",
        body.content,
    )
    return present.comment(
        row,
        account.sonolus_id,
        False,
        {account.sonolus_id: account},
        tags,
        context.storage,
    )


@router.delete("/comments/{comment_id}", response_model=schemas.Comment)
async def delete_comment(
    chart_id: str,
    comment_id: Annotated[int, Path(ge=1, le=2147483647)],
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(scope="comment:write", banned=False))],
) -> schemas.Comment:
    """Delete a comment; staff may delete anyone's.

    Args:
        chart_id: Chart id.
        comment_id: Comment id.
        context: App context.
        principal: The caller.

    Returns:
        schemas.Comment: The deleted comment, including its text.
    """
    async with context.database.transaction() as connection:
        await load_chart(connection, principal, chart_id)
        row = await comments_db.get_comment(connection, chart_id, comment_id)
        if row is None or row.deleted_at is not None:
            raise not_found("error.comment.not_found")
        own = principal.sonolus_id is not None and principal.sonolus_id == row.commenter
        if not own:
            principal.require_permission("comment.delete")
            if row.commenter:
                principal.require_outranks(
                    await permissions_db.get_permissions(connection, row.commenter)
                )
        if not await comments_db.soft_delete_comment(connection, comment_id):
            raise not_found("error.comment.not_found")
        if not own:
            await staff_log.record(
                context,
                connection,
                principal,
                "comment_delete",
                "comment",
                str(comment_id),
                row.content,
                None,
            )
        deleted = await comments_db.get_comment(connection, chart_id, comment_id)
        authors = [deleted.commenter] if deleted and deleted.commenter else []
        accounts = await accounts_db.get_accounts(connection, authors)
        tags = await account_tags.load(connection, authors, context.storage)
    assert deleted is not None
    return present.comment(deleted, principal.sonolus_id, True, accounts, tags, context.storage)


@router.get("/leaderboards", response_model=schemas.LeaderboardPage)
async def list_records(
    chart_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
    page: Annotated[int, Query(ge=0, le=1000000)] = 0,
    limit: Annotated[int, Query()] = 3,
    leaderboard_type: LeaderboardType = "arcade_score_speed",
) -> schemas.LeaderboardPage:
    """List a chart's leaderboard.

    Args:
        chart_id: Chart id.
        context: App context.
        principal: The caller.
        page: Zero-based page.
        limit: Records per page.
        leaderboard_type: Ranking.

    Returns:
        schemas.LeaderboardPage: The page.
    """
    if limit not in (3, 10):
        raise bad_request(
            "error.request.invalid",
            fields=[{"location": ["query", "limit"], "type": "literal_error"}],
        )
    async with context.database.connection() as connection:
        await load_chart(connection, principal, chart_id)
        rows, total = await leaderboards_db.list_chart_records(
            connection, chart_id, leaderboard_type, page, limit
        )
        submitters = [row.submitter for row in rows]
        accounts = await accounts_db.get_accounts(connection, submitters)
        tags = await account_tags.load(connection, submitters, context.storage)
    return schemas.LeaderboardPage(
        items=[
            present.record(row, context.storage, principal.sonolus_id, accounts, tags)
            for row in rows
        ],
        page=page,
        page_count=present.page_count(total, limit),
        can_moderate=principal.has_permission("leaderboard.delete"),
    )


@router.get("/leaderboards/{record_id}", response_model=schemas.RecordDetails)
async def get_record(
    chart_id: str,
    record_id: Annotated[int, Path(ge=1, le=2147483647)],
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
) -> schemas.RecordDetails:
    """Return a leaderboard record with its chart.

    Args:
        chart_id: Chart id.
        record_id: Record id.
        context: App context.
        principal: The caller.

    Returns:
        schemas.RecordDetails: The record.
    """
    async with context.database.connection() as connection:
        access = await load_chart(connection, principal, chart_id)
        row = await leaderboards_db.get_record(connection, chart_id, record_id)
        if row is None:
            raise not_found("error.record.not_found")
        accounts = await accounts_db.get_accounts(connection, [row.submitter])
        tags = await account_tags.load(connection, [row.submitter], context.storage)
    own = principal.sonolus_id == row.submitter
    return schemas.RecordDetails(
        record=present.record(row, context.storage, principal.sonolus_id, accounts, tags),
        chart=present.chart(access.chart, context.storage),
        can_delete=own or principal.has_permission("leaderboard.delete"),
    )


@router.delete("/leaderboards/{record_id}", response_model=schemas.RecordDetails)
async def delete_record(
    chart_id: str,
    record_id: Annotated[int, Path(ge=1, le=2147483647)],
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(scope="leaderboard:delete"))],
) -> schemas.RecordDetails:
    """Delete a leaderboard record and its replay; staff may delete anyone's.

    Args:
        chart_id: Chart id.
        record_id: Record id.
        context: App context.
        principal: The caller.

    Returns:
        schemas.RecordDetails: The deleted record.
    """
    async with context.database.transaction() as connection:
        access = await load_chart(connection, principal, chart_id, allow_hidden=True)
        row = await leaderboards_db.get_record(connection, chart_id, record_id)
        if row is None:
            raise not_found("error.record.not_found")
        own = principal.sonolus_id is not None and principal.sonolus_id == row.submitter
        if not own:
            principal.require_permission("leaderboard.delete")
            principal.require_outranks(
                await permissions_db.get_permissions(connection, row.submitter)
            )
        deleted = await leaderboards_db.delete_record(connection, record_id)
        if deleted is None:
            raise not_found("error.record.not_found")
        accounts = await accounts_db.get_accounts(connection, [deleted.submitter])
        if not own:
            await staff_log.record(
                context,
                connection,
                principal,
                "leaderboard_delete",
                "record",
                str(record_id),
                json.dumps(
                    {
                        "chart_id": deleted.chart_id,
                        "submitter": deleted.submitter,
                        "arcade_score": deleted.arcade_score,
                        "accuracy_score": deleted.accuracy_score,
                    }
                ),
                None,
            )
    await context.storage.discard_keys(
        [
            deleted.replay_prefix + deleted.replay_data_hash,
            deleted.replay_prefix + deleted.replay_config_hash,
        ]
    )
    return schemas.RecordDetails(
        record=present.record(
            deleted,
            context.storage,
            principal.sonolus_id,
            accounts,
            present.AccountTags(),
        ),
        chart=present.chart(access.chart, context.storage),
        can_delete=True,
    )


def _require_collaborator_view(access: ChartAccess) -> None:
    """Fail unless the caller owns or collaborates on the chart.

    Args:
        access: Chart access.
    """
    if not access.is_owner and not access.chart_permissions:
        raise forbidden("error.collaboration.not_member")


def _check_grantable(access: ChartAccess, permissions: list[str]) -> None:
    """Fail when the caller can't grant these permissions.

    Args:
        access: Chart access.
        permissions: Permissions to grant.
    """
    if access.is_owner:
        return
    if not access.has_chart_permission("manage"):
        raise forbidden("error.chart.permission_required", permission="manage")
    if "manage" in permissions or not set(permissions) <= access.chart_permissions:
        raise forbidden("error.collaboration.cannot_grant")


@router.get("/collaborators", response_model=schemas.CollaboratorList)
async def list_collaborators(
    chart_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="chart:collaborate"))],
) -> schemas.CollaboratorList:
    """List a chart's collaborators and, for managers, pending invites.

    Args:
        chart_id: Chart id.
        context: App context.
        principal: The caller.

    Returns:
        schemas.CollaboratorList: The collaborators.
    """
    async with context.database.connection() as connection:
        access = await load_chart(connection, principal, chart_id)
        _require_collaborator_view(access)
        rows = await collaborators_db.list_collaborators(connection, chart_id)
        invites = (
            await collaborators_db.list_invites_for_chart(connection, chart_id)
            if access.has_chart_permission("manage")
            else []
        )
    return schemas.CollaboratorList(
        collaborators=[
            schemas.Collaborator(
                account=schemas.AccountSummary(
                    id=row.sonolus_id,
                    handle=row.sonolus_handle,
                    username=row.sonolus_username,
                ),
                permissions=row.permissions,
                added_at=row.added_at,
            )
            for row in rows
        ],
        invites=[
            schemas.CollaboratorInvite(
                id=invite.id,
                chart_id=invite.chart_id,
                chart_title=invite.chart_title,
                account_id=invite.sonolus_id,
                permissions=invite.permissions,
                invited_by=invite.invited_by,
                created_at=invite.created_at,
            )
            for invite in invites
        ],
    )


@router.post("/collaborators", status_code=201, response_model=schemas.CollaboratorInvite)
async def invite_collaborator(
    chart_id: str,
    body: InviteBody,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal, Depends(auth(account=True, scope="chart:collaborate", banned=False))
    ],
) -> schemas.CollaboratorInvite:
    """Invite an account to collaborate on a chart.

    Args:
        chart_id: Chart id.
        body: The invite.
        context: App context.
        principal: The caller.

    Returns:
        schemas.CollaboratorInvite: The invite.
    """
    account = principal.require_account()
    async with context.database.transaction() as connection:
        access = await load_chart(connection, principal, chart_id)
        permissions = with_read(body.permissions)
        _check_grantable(access, permissions)
        if body.account_id == access.chart.author:
            raise bad_request("error.collaboration.owner")
        if (
            await collaborators_db.get_collaborator(connection, chart_id, body.account_id)
            is not None
        ):
            raise conflict("error.collaboration.already_member")
        invitee = await accounts_db.get_account(connection, body.account_id)
        if invitee is None or invitee.deleted_at is not None:
            raise not_found("error.account.not_found")
        if invitee.banned:
            raise conflict("error.collaboration.account_banned")
        if not (await settings_db.get_settings(connection, body.account_id)).collaboration_invites:
            raise forbidden("error.collaboration.invites_disabled")
        invite_id, created = await collaborators_db.create_invite(
            connection, chart_id, body.account_id, permissions, account.sonolus_id
        )
        if created:
            await notifications_db.create_notification(
                connection,
                body.account_id,
                "Collaboration invite",
                f"{account.sonolus_username}#{account.sonolus_handle} invited you to collaborate on {access.chart.title}.",
            )
        invites = await collaborators_db.list_invites_for_chart(connection, chart_id)
    invite = next(item for item in invites if item.id == invite_id)
    return schemas.CollaboratorInvite(
        id=invite.id,
        chart_id=invite.chart_id,
        chart_title=invite.chart_title,
        account_id=invite.sonolus_id,
        permissions=invite.permissions,
        invited_by=invite.invited_by,
        created_at=invite.created_at,
    )


@router.patch("/collaborators/{account_id}", status_code=204)
async def update_collaborator(
    chart_id: str,
    account_id: str,
    body: CollaboratorPermissions,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal, Depends(auth(account=True, scope="chart:collaborate", banned=False))
    ],
) -> Response:
    """Change a collaborator's permissions.

    Args:
        chart_id: Chart id.
        account_id: Collaborator account id.
        body: New permissions.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    async with context.database.transaction() as connection:
        access = await load_chart(connection, principal, chart_id)
        permissions = with_read(body.permissions)
        _check_grantable(access, permissions)
        target = await collaborators_db.get_collaborator(connection, chart_id, account_id)
        if target is None:
            raise not_found("error.collaboration.not_member")
        if not access.is_owner and "manage" in target.permissions:
            raise forbidden("error.collaboration.cannot_manage_manager")
        await collaborators_db.set_permissions(connection, chart_id, account_id, permissions)
    return Response(status_code=204)


@router.delete("/collaborators/{account_id}", status_code=204)
async def remove_collaborator(
    chart_id: str,
    account_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="chart:collaborate"))],
) -> Response:
    """Remove a collaborator, cancel an invite, or leave a chart.

    Args:
        chart_id: Chart id.
        account_id: Collaborator or invited account id.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    account = principal.require_account()
    async with context.database.transaction() as connection:
        access = await load_chart(connection, principal, chart_id)
        leaving = account.sonolus_id == account_id
        if not leaving and not access.is_owner:
            if not access.has_chart_permission("manage"):
                raise forbidden("error.chart.permission_required", permission="manage")
            target = await collaborators_db.get_collaborator(connection, chart_id, account_id)
            if target is not None and "manage" in target.permissions:
                raise forbidden("error.collaboration.cannot_manage_manager")
        removed = await collaborators_db.remove_collaborator(connection, chart_id, account_id)
        cancelled = await collaborators_db.cancel_invite(connection, chart_id, account_id)
        if not removed and not cancelled:
            raise not_found("error.collaboration.not_member")
    return Response(status_code=204)
