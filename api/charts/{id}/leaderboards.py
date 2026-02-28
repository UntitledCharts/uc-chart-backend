"""
Unlike charts/leaderboards, returns leaderboards for a specific level
"""

from io import BytesIO
from fastapi import (
    APIRouter,
    File,
    Form,
    Request,
    HTTPException,
    status,
    UploadFile,
    Query,
)
import asyncio
import gzip
from typing import Literal

from helpers.models import ReplayData, LeaderboardRecord, leaderboard_type
from helpers.session import Session, get_session
from helpers.hashing import calculate_sha1
from core import ChartFastAPI

from database import leaderboards, charts, accounts

router = APIRouter()


def speed_multiplier(speed: float | None) -> float:
    """
    Only used for comparing scores
    Sorting is in SQL
    """
    if speed is None:
        return 1.0

    tier = int(speed * 10) / 10

    if tier < 1:
        return tier - 0.4
    else:
        return 1.0 + ((tier - 1.0) * 0.075)


@router.post("/")
async def upload_replay(
    id: str,
    request: Request,
    user_id: str = Form(...),
    display_name: str = Form(...),
    engine_name: str = Form(...),
    speed: float | None = Form(None),
    replay_data: UploadFile = File(...),
    replay_config: UploadFile = File(...),
):
    app: ChartFastAPI = request.app

    if request.headers.get(app.auth_header) != app.auth:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="the")

    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )

    data = await replay_data.read()
    config = await replay_config.read()

    replay = ReplayData.model_validate_json(gzip.decompress(data))

    tasks = []
    async with app.db_acquire() as conn:
        chart = await conn.fetchrow(charts.get_chart_by_id(id))

        if chart.status == "PRIVATE" and chart.author != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="This chart is private."
            )

        curr_record = await conn.fetchrow(
            leaderboards.get_user_leaderboard_record_for_chart(id, user_id)
        )

        if curr_record:
            if int(
                curr_record.arcade_score * speed_multiplier(curr_record.speed)
            ) >= int(replay.result.arcadeScore * speed_multiplier(speed)):
                return {"status": "unchanged"}

            await conn.execute(leaderboards.delete_leaderboard_record(curr_record.id))

    replay_data_hash = calculate_sha1(data)
    replay_config_hash = calculate_sha1(config)

    async with app.s3_session_getter() as s3:
        bucket = await s3.Bucket(app.s3_bucket)

        for contents, hash in ((data, replay_data_hash), (config, replay_config_hash)):
            tasks.append(
                bucket.upload_fileobj(
                    Fileobj=BytesIO(contents),
                    Key=f"{chart.author}/{chart.id}/replays/{user_id}/{hash}",
                    ExtraArgs={"ContentType": "application/gzip"},
                )
            )

        if curr_record:
            batch = [
                {
                    "Key": f"{chart.author}/{chart.id}/replays/{user_id}/{curr_record.replay_data_hash}"
                },
                {
                    "Key": f"{chart.author}/{chart.id}/replays/{user_id}/{curr_record.replay_config_hash}"
                },
            ]

            tasks.append(bucket.delete_objects(Delete={"Objects": batch}))

        await asyncio.gather(*tasks)

    async with app.db_acquire() as conn:
        await conn.execute(
            leaderboards.create_leaderboard_record(
                LeaderboardRecord(
                    submitter=user_id,
                    replay_data_hash=replay_data_hash,
                    replay_config_hash=replay_config_hash,
                    chart_id=id,
                    engine=engine_name,
                    grade=replay.result.grade,
                    nperfect=replay.result.perfect,
                    ngreat=replay.result.great,
                    ngood=replay.result.good,
                    nmiss=replay.result.miss,
                    arcade_score=replay.result.arcadeScore,
                    accuracy_score=replay.result.accuracyScore,
                    speed=speed,
                    display_name=display_name,
                    public_chart=chart.status == "PUBLIC",
                )
            )
        )

    return {"status": "ok"}


@router.get("/")
async def get_leaderboards(
    request: Request,
    id: str,
    page: int = Query(0, ge=0),
    limit: Literal["3", "10"] = "3",
    leaderboard_type: leaderboard_type = "arcade_score_speed",
    session: Session = get_session(),
):
    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )

    app: ChartFastAPI = request.app

    limit = int(limit)
    leaderboards_query, count_query = leaderboards.get_leaderboards_for_chart(
        id, limit, page, leaderboard_type, session.sonolus_id
    )

    async with app.db_acquire() as conn:
        count = await conn.fetchrow(count_query)

        if count.total_count == 0:
            data = []
            page_count = 0
        elif page * 10 >= count.total_count:
            data = []
            page_count = (count.total_count + 9) // 10
        else:
            records = await conn.fetch(leaderboards_query)

            account_dict = {
                account.sonolus_id: account
                for account in await conn.fetch(
                    accounts.get_public_account_batch(
                        list(set([record.submitter for record in records]))
                    )
                )
            }

            data = [
                {**row.model_dump(), "account": account_dict.get(row.submitter)}
                for row in records
            ]
            page_count = (count.total_count + 9) // 10

    return {"pageCount": page_count, "data": data}


@router.get("/{record_id}/")
async def get_record(
    request: Request, id: str, record_id: int, session: Session = get_session()
):
    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )

    app: ChartFastAPI = request.app

    async with app.db_acquire() as conn:
        leaderboard_record = await conn.fetchrow(
            leaderboards.get_leaderboard_record_by_id(id, record_id, session.sonolus_id)
        )

        if not leaderboard_record:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        chart = await conn.fetchrow(charts.get_chart_by_id(leaderboard_record.chart_id))
        submitter = await conn.fetchrow(
            accounts.get_public_account(leaderboard_record.submitter)
        )

        if not chart:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="unknown chart",
            )

        if chart.status == "PRIVATE" and chart.author != session.sonolus_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You don't have access to this chart",
            )

    data = leaderboard_record.model_dump()

    if session.sonolus_id:
        user = await session.user()
        data["mod"] = user.mod or user.admin

    return {
        "data": data,
        "chart": chart,
        "submitter": submitter,
        "asset_base_url": app.s3_asset_base_url,
    }


@router.delete("/{record_id}/")
async def delete_record(
    request: Request,
    id: str,
    record_id: int,
    session: Session = get_session(enforce_auth=True),
):
    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )

    app: ChartFastAPI = request.app

    async with app.db_acquire() as conn:
        leaderboard_record = await conn.fetchrow(
            leaderboards.get_leaderboard_record_by_id(id, record_id, session.sonolus_id)
        )

        if not leaderboard_record:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        user = await session.user()
        if not (leaderboard_record.owner or user.mod or user.admin):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

        await conn.execute(leaderboards.delete_leaderboard_record(record_id))

        data = leaderboard_record.model_dump()

        user = await session.user()
        mod = user.mod or user.admin
        data["mod"] = mod

        if mod:
            chart = await conn.fetchrow(
                charts.get_chart_by_id(leaderboard_record.chart_id)
            )
            data["chart_title"] = chart.title

    return data
