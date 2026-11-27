from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.auth.principal import Principal, auth
from app.auth.tokens import hmac_hex
from app.context import AppContext, get_context
from app.database import accounts as accounts_db
from app.database import charts as charts_db
from app.database import leaderboards as leaderboards_db
from app.database import permissions as permissions_db
from app.database import promotions as promotions_db
from app.models import present, schemas
from app.services import promotion_targeting
from app.services.chart_access import check_chart_id
from app.services.client_ip import client_ip

router = APIRouter(prefix="/v1", tags=["global"])

VIEWER_IP_HEADER = "x-viewer-ip"


class ClickBody(schemas.Schema):
    """A click on a served promotion.

    Attributes:
        chart_id: Promoted chart id.
        view_code: View code from the serve response.
    """

    chart_id: str
    view_code: str


class ClickResult(BaseModel):
    """Whether a click was counted.

    Attributes:
        counted: True when the click counted towards the promotion.
    """

    counted: bool


async def _global_records(
    context: AppContext, principal: Principal, rows: list[leaderboards_db.RecordRow]
) -> list[schemas.GlobalRecord]:
    """Attach charts and submitters to global records.

    Args:
        context: App context.
        principal: The caller.
        rows: Record rows.

    Returns:
        list[schemas.GlobalRecord]: The records.
    """
    async with context.database.connection() as connection:
        charts = await charts_db.get_charts(connection, [row.chart_id for row in rows])
        submitters = [row.submitter for row in rows]
        accounts = await accounts_db.get_accounts(connection, submitters)
        tags = await permissions_db.get_permissions_for(connection, submitters)
    return [
        schemas.GlobalRecord(
            record=present.record(row, context.storage, principal.sonolus_id, accounts, tags),
            chart=present.chart(charts[row.chart_id], context.storage),
        )
        for row in rows
        if row.chart_id in charts
    ]


@router.get("/leaderboards/recent", response_model=schemas.Page[schemas.GlobalRecord])
async def recent_records(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
    page: Annotated[int, Query(ge=0, le=1000000)] = 0,
    limit: Annotated[int, Query(gt=0, le=10)] = 10,
) -> schemas.Page[schemas.GlobalRecord]:
    """List the newest records on public charts.

    Args:
        context: App context.
        principal: The caller.
        page: Zero-based page.
        limit: Records per page.

    Returns:
        schemas.Page[schemas.GlobalRecord]: The page.
    """
    async with context.database.connection() as connection:
        rows, total = await leaderboards_db.list_recent_public_records(connection, page, limit)
    return schemas.Page[schemas.GlobalRecord](
        items=await _global_records(context, principal, rows),
        page=page,
        page_count=present.page_count(total, limit),
    )


@router.get("/leaderboards/random", response_model=list[schemas.GlobalRecord])
async def random_records(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
    limit: Annotated[int, Query(gt=0, le=10)] = 10,
) -> list[schemas.GlobalRecord]:
    """Pick random records on public charts.

    Args:
        context: App context.
        principal: The caller.
        limit: Records to return.

    Returns:
        list[schemas.GlobalRecord]: The records.
    """
    async with context.database.connection() as connection:
        rows = await leaderboards_db.random_public_records(connection, limit)
    return await _global_records(context, principal, rows)


def viewer_key(context: AppContext, principal: Principal, request: Request) -> str | None:
    """Identify a promotion viewer for view deduplication.

    Args:
        context: App context.
        principal: The caller.
        request: Current request.

    Returns:
        str | None: Hashed viewer identity, or None when the viewer can't be identified.
    """
    if principal.account is not None and not principal.is_oauth:
        return "a:" + principal.account.sonolus_id
    address = request.headers.get(VIEWER_IP_HEADER) if principal.sonoserver else None
    address = address or client_ip(request, context.config.server.trusted_proxies)
    if address is None:
        return None
    return "i:" + hmac_hex(context.config.keys.token_secret, "viewer:" + address)


@router.post("/promotions/serve", response_model=schemas.PromotionServe)
async def serve_promotion(
    request: Request,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
) -> schemas.PromotionServe:
    """Maybe pick a promotion to show, counting the view once per viewer and window.

    Args:
        request: Current request.
        context: App context.
        principal: The caller.

    Returns:
        schemas.PromotionServe: The promotion, or none.
    """
    constants = context.constants.promotions
    account = principal.account if not principal.is_oauth else None
    async with context.database.connection() as connection:
        active = await promotions_db.active_promotions(connection)
        if not active:
            return schemas.PromotionServe()
        shown = (
            await promotions_db.shown_counts(
                connection, account.sonolus_id, [item.id for item in active]
            )
            if account
            else {}
        )
    chosen = promotion_targeting.choose_promotion(account, active, shown, constants)
    if chosen is None:
        return schemas.PromotionServe()
    key = viewer_key(context, principal, request)
    view_code: str | None = None
    async with context.database.transaction() as connection:
        if key is not None:
            view_code = await promotions_db.existing_view_code(
                connection, chosen.id, key, constants.view_window_hours
            )
        if view_code is None:
            for _ in range(constants.view_code_attempts):
                candidate = promotion_targeting.generate_view_code(constants.view_code_length)
                try:
                    async with connection.transaction():
                        counted = await promotions_db.claim_view(
                            connection,
                            chosen.id,
                            key or ("n:" + candidate),
                            candidate,
                            constants.view_window_hours,
                        )
                except promotions_db.ViewCodeCollision:
                    continue
                if counted:
                    view_code = candidate
                    if account is not None:
                        await accounts_db.record_ad_view(connection, account.sonolus_id)
                        await promotions_db.increment_user_shown(
                            connection, chosen.id, account.sonolus_id
                        )
                    break
                if key is not None:
                    view_code = await promotions_db.existing_view_code(
                        connection, chosen.id, key, constants.view_window_hours
                    )
                    if view_code is not None:
                        break
        if view_code is None:
            return schemas.PromotionServe()
        chart = await charts_db.get_chart(connection, chosen.chart_id, principal.sonolus_id)
    if chart is None:
        return schemas.PromotionServe()
    return schemas.PromotionServe(
        promotion=schemas.ServedPromotion(
            chart=present.chart(chart, context.storage), view_code=view_code
        )
    )


@router.post("/promotions/clicks", response_model=ClickResult)
async def click_promotion(
    body: ClickBody,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
) -> ClickResult:
    """Count a viewer opening a served promotion.

    Args:
        body: The click.
        context: App context.
        principal: The caller.

    Returns:
        ClickResult: Whether it counted.
    """
    check_chart_id(body.chart_id)
    account = principal.account if not principal.is_oauth else None
    async with context.database.connection() as connection:
        counted = await promotions_db.resolve_click(
            connection,
            body.chart_id,
            body.view_code,
            account is not None,
            context.constants.promotions.view_window_hours,
        )
        if counted and account is not None:
            await accounts_db.record_ad_click(connection, account.sonolus_id)
    return ClickResult(counted=counted)


@router.get("/limits", response_model=schemas.Limits)
async def get_limits(
    context: Annotated[AppContext, Depends(get_context)],
) -> schemas.Limits:
    """Return upload limits.

    Args:
        context: App context.

    Returns:
        schemas.Limits: The limits.
    """
    constants = context.constants
    sizes = constants.files.max_sizes
    text = dict(constants.text.max_lengths)
    text["tags_count"] = constants.text.max_tags
    return schemas.Limits(
        files={
            key: sizes[key]
            for key in (
                "jacket",
                "chart",
                "audio",
                "preview",
                "background",
                "avatar",
                "banner",
            )
        },
        text=text,
        rating={
            "min": constants.rating.min,
            "max": constants.rating.max,
            "decimal_places": constants.rating.decimal_places,
        },
    )
