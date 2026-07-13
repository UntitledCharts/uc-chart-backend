from typing import Optional
from core import ChartFastAPI

from fastapi import APIRouter, Request, HTTPException, status
from helpers.session import get_session, Session

from database import charts, leaderboards, staff_actions

from helpers.models import ChartVisibilityData, ChartScheduleData
from helpers.webhook_handler import WebhookMessage, WebhookEmbed
from helpers.sanitizers import sanitize_md
from helpers.urls import url_creator

router = APIRouter()


@router.patch("/schedule-public")
async def main(
    request: Request,
    id: str,
    data: ChartScheduleData,
    session: Session = get_session(
        enforce_auth=True,
        enforce_type=False,
        allow_banned_users=False,
        scopes=["chart:visibility"],
    ),
):
    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )

    app: ChartFastAPI = request.app
    user = await session.user()
    # oauth tokens never get mod powers, only the user's own charts
    is_mod = user.mod and not session.is_oauth

    query = charts.get_chart_by_id(id, sonolus_id=user.sonolus_id)
    async with app.db_acquire() as conn:
        chart = await conn.fetchrow(query)
    if not chart:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Chart not found."
        )
    if chart.status == "PUBLIC":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot schedule a chart that is already public.",
        )

    # Heuristic: treat big numbers as ms
    publish_time = data.publish_time
    publish_time_seconds: Optional[int] = None
    if publish_time is not None:
        if publish_time < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="publish_time must be a valid epoch timestamp or null.",
            )
        publish_time_seconds = (
            publish_time // 1000 if publish_time > 10**12 else publish_time
        )

    if is_mod:
        query = charts.update_scheduled_publish(
            chart_id=id,
            publish_time_seconds=publish_time_seconds,
        )
    else:
        query = charts.update_scheduled_publish(
            chart_id=id,
            sonolus_id=user.sonolus_id,
            publish_time_seconds=publish_time_seconds,
        )

    async with app.db_acquire() as conn:
        result = await conn.fetchrow(query)
        if not result:
            if is_mod:
                raise HTTPException(
                    status=status.HTTP_404_NOT_FOUND,
                    detail=f'Chart with ID "{id}" not found for any user!',
                )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f'Chart with ID "{id}" not found for this user.',
            )

        d = result.model_dump()

        # ✅ Replace "visibility change" webhook with "schedule change" webhook
        webhook_url = app.config["discord"]["all-visibility-changes-webhook"].strip()
        if webhook_url != "":
            wmsg = WebhookMessage(
                webhook_url,
                app.config["discord"]["avatar-url"],
                app.config["discord"]["username"],
            )

            if publish_time_seconds is None:
                desc = (
                    f"Scheduled publish was **removed** for chart `{sanitize_md(result.title)}` "
                    f"(`{sanitize_md(result.author_full)}`).\n\n"
                    f"{url_creator(app.config['server']['sonolus-server-url'], 'levels', app.config['server']['sonolus-server-chart-prefix'] + result.id, as_sonolus_open=True)}"
                )
                color = "ORANGE"
            else:
                desc = (
                    f"New **scheduled publish** set for chart `{sanitize_md(result.title)}` "
                    f"(`{sanitize_md(result.author_full)}`).\n\n"
                    f"Publish time: <t:{publish_time_seconds}:F>  (<t:{publish_time_seconds}:R>)\n\n"
                    f"{url_creator(app.config['server']['sonolus-server-url'], 'levels', app.config['server']['sonolus-server-chart-prefix'] + result.id, as_sonolus_open=True)}"
                )
                color = "GREEN"

            wembed = (
                WebhookEmbed()
                .set_title("Chart schedule updated")
                .set_description(desc)
                .set_timestamp(True)
                .set_thumbnail(
                    url_creator(
                        app.s3_asset_base_url,
                        result.author,
                        result.id,
                        result.jacket_file_hash,
                    )
                )
                .set_color(color)
            )
            wmsg.add_embed(wembed)
            await wmsg.send()
        if is_mod:
            d["mod"] = True
        if user.sonolus_id == d["author"]:
            d["owner"] = True
        return d


@router.patch("/")
async def main(
    request: Request,
    id: str,
    data: ChartVisibilityData,
    session: Session = get_session(
        enforce_auth=True,
        enforce_type=False,
        allow_banned_users=False,
        scopes=["chart:visibility"],
    ),
):
    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )
    app: ChartFastAPI = request.app
    user = await session.user()
    # oauth tokens never get mod powers, only the user's own charts
    is_mod = user.mod and not session.is_oauth

    if is_mod:
        query = charts.update_status(chart_id=id, status=data.status)
    else:
        query = charts.update_status(
            chart_id=id, sonolus_id=user.sonolus_id, status=data.status
        )

    async with app.db_acquire() as conn:
        result = await conn.fetchrow(query)
        if result:
            await conn.execute(
                leaderboards.update_leaderboard_visibility(
                    chart_id=id, status=data.status
                )
            )

            if is_mod and user.sonolus_id != result.author:
                await conn.execute(
                    staff_actions.log_action(
                        actor_id=user.sonolus_id,
                        action="visibility_change",
                        target_type="chart",
                        target_id=id,
                        previous_value=result.status,
                        new_value=data.status,
                    )
                )

            d = result.model_dump()
            if app.config["discord"]["all-visibility-changes-webhook"].strip() != "":
                wmsg = WebhookMessage(
                    app.config["discord"]["all-visibility-changes-webhook"],
                    app.config["discord"]["avatar-url"],
                    app.config["discord"]["username"],
                )
                wembed = (
                    WebhookEmbed()
                    .set_title("Chart visibility change")
                    .set_description(
                        f"The chart `{sanitize_md(result.title)}` (`{sanitize_md(result.author_full)}`) was changed to `{data.status}` from `{result.status}`.\n\n{url_creator(app.config['server']['sonolus-server-url'], 'levels', app.config['server']['sonolus-server-chart-prefix'] + result.id, as_sonolus_open=True)}"
                    )
                    .set_timestamp(True)
                    .set_thumbnail(
                        url_creator(
                            app.s3_asset_base_url,
                            result.author,
                            result.id,
                            result.jacket_file_hash,
                        )
                    )
                    .set_color(
                        "RED"
                        if data.status == "PRIVATE"
                        else "ORANGE" if data.status == "UNLISTED" else "GREEN"
                    )
                )
                wmsg.add_embed(wembed)
                await wmsg.send()
            if is_mod:
                d["mod"] = True
            if user.sonolus_id == d["author"]:
                d["owner"] = True
            return d
        if is_mod:
            raise HTTPException(
                status=status.HTTP_404_NOT_FOUND,
                detail=f'Chart with ID "{id}" not found for any user!',
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f'Chart with ID "{id}" not found for this user.',
        )
