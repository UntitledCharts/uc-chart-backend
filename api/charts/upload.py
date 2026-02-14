import uuid, io, asyncio, json, time, gzip

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request, HTTPException, status, UploadFile, Form
from fastapi.responses import JSONResponse

from database import charts, accounts

from helpers.models import ChartUploadData, Chart
from helpers.hashing import calculate_sha1
from helpers.file_checks import get_and_check_file
from helpers.backgrounds import generate_backgrounds_resize_jacket
from helpers.session import get_session, Session
from helpers.constants import MAX_FILE_SIZES, MAX_TEXT_SIZES, MAX_RATINGS

import sonolus_converters

from typing import Optional

from pydantic import ValidationError

from core import ChartFastAPI

router = APIRouter()


@router.post("/")
async def main(
    request: Request,
    jacket_image: UploadFile,
    chart_file: UploadFile,
    audio_file: UploadFile,
    data: str = Form(...),
    preview_file: Optional[UploadFile] = None,
    background_image: Optional[UploadFile] = None,
    session: Session = get_session(
        enforce_auth=True, enforce_type="external", allow_banned_users=False
    ),
):
    app: ChartFastAPI = request.app
    try:
        data: ChartUploadData = ChartUploadData.model_validate_json(data)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors())

    if (
        (data.description and len(data.description) > MAX_TEXT_SIZES["description"])
        or (data.artists and len(data.artists) > MAX_TEXT_SIZES["artists"])
        or (data.title and len(data.title) > MAX_TEXT_SIZES["title"])
        or (data.author and len(data.author) > MAX_TEXT_SIZES["author"])
        or (
            data.tags and any(len(tag) > MAX_TEXT_SIZES["per_tag"] for tag in data.tags)
        )
        or (data.tags and len(data.tags) > MAX_TEXT_SIZES["tags_count"])
        or (data.rating > MAX_RATINGS["max"])
        or (data.rating < MAX_RATINGS["min"])
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Length limits exceeded"
        )

    user = await session.user()

    if False:  # XXX: check and confirm
        if user.oauth_details:
            oauth = json.loads(user.oauth_details)
        else:
            oauth = {}
        discord_oauth = oauth.get("discord")

        if not discord_oauth:
            return JSONResponse(content={}, status_code=403)
        now = int(time.time())
        if now >= discord_oauth.get("expires_at", 0):
            return JSONResponse(content={}, status_code=403)

        user_resp = await app.oauth.discord.get("users/@me", token=discord_oauth)
        if user_resp.status_code != 200:
            # delete oauth if invalid
            query = accounts.delete_oauth(session.sonolus_id, "discord")
            async with app.db_acquire() as conn:
                await conn.execute(query)
            return JSONResponse(content={}, status_code=403)

        guilds_resp = await app.oauth.discord.get(
            "users/@me/guilds", token=discord_oauth
        )
        if guilds_resp.status_code != 200:
            return JSONResponse(content={}, status_code=403)
        guilds_data = await guilds_resp.json()
        required_guild = app.config["oauth"]["required-discord-server"]
        in_guild = any(guild["id"] == required_guild for guild in guilds_data)
        if not in_guild:
            return JSONResponse(content={}, status_code=403)

    cooldown = user.chart_upload_cooldown
    if cooldown and not app.debug:
        now = datetime.now(timezone.utc)
        if now < cooldown:
            remaining = cooldown - now
            minutes, seconds = divmod(int(remaining.total_seconds()), 60)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"On cooldown. Time remaining: {minutes}m {seconds}s",
            )
    if (
        jacket_image.size > MAX_FILE_SIZES["jacket"]
        or chart_file.size > MAX_FILE_SIZES["chart"]
        or audio_file.size > MAX_FILE_SIZES["audio"]
    ):
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Uploaded files exceed file size limit.",
        )
    if preview_file:
        if preview_file.size > MAX_FILE_SIZES["preview"]:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Uploaded files exceed file size limit.",
            )
    if background_image:
        if background_image.size > MAX_FILE_SIZES["background"]:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Uploaded files exceed file size limit.",
            )

    s3_uploads = []
    chart_id = str(uuid.uuid4()).replace("-", "")

    jacket_bytes = await get_and_check_file(jacket_image, "image")
    v1, v3, jacket_bytes = await app.run_blocking(
        generate_backgrounds_resize_jacket, jacket_bytes
    )
    jacket_hash = calculate_sha1(jacket_bytes)
    v1_hash = calculate_sha1(v1)
    s3_uploads.append(
        {
            "path": f"{session.sonolus_id}/{chart_id}/{v1_hash}",
            "hash": v1_hash,
            "bytes": v1,
            "content-type": "image/png",
        }
    )
    v3_hash = calculate_sha1(v3)
    s3_uploads.append(
        {
            "path": f"{session.sonolus_id}/{chart_id}/{v3_hash}",
            "hash": v3_hash,
            "bytes": v3,
            "content-type": "image/png",
        }
    )
    s3_uploads.append(
        {
            "path": f"{session.sonolus_id}/{chart_id}/{jacket_hash}",
            "hash": jacket_hash,
            "bytes": jacket_bytes,
            "content-type": "image/png",
        }
    )

    valid, sus, usc, leveldata, compressed, ld_type = sonolus_converters.detect(
        (await chart_file.read())
    )
    await chart_file.seek(0)
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid file format."
        )
    chart_bytes = await chart_file.read()

    def convert() -> bytes:
        if sus:
            converted = io.BytesIO()
            score = sonolus_converters.sus.load(
                io.TextIOWrapper(io.BytesIO(chart_bytes), encoding="utf-8")
            )
            sonolus_converters.next_sekai.export(converted, score)
        elif usc:
            converted = io.BytesIO()
            score = sonolus_converters.usc.load(
                io.TextIOWrapper(io.BytesIO(chart_bytes), encoding="utf-8")
            )
            sonolus_converters.next_sekai.export(converted, score)
        elif leveldata:
            if ld_type != "nextsekai" and not app.debug:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Incorrect LevelData: {ld_type} (expected: NextSekai)",
                )
            if not compressed:
                compressed_data = io.BytesIO()
                with gzip.GzipFile(
                    fileobj=compressed_data, mode="wb", filename="LevelData", mtime=0
                ) as f:
                    f.write(chart_bytes)
                compressed_data.seek(0)
                return compressed_data.getvalue()
            return chart_bytes
        return converted.read()

    try:
        chart_bytes = await app.run_blocking(convert)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    chart_hash = calculate_sha1(chart_bytes)
    s3_uploads.append(
        {
            "path": f"{session.sonolus_id}/{chart_id}/{chart_hash}",
            "hash": chart_hash,
            "bytes": chart_bytes,
            "content-type": "application/gzip",
        }
    )

    audio_bytes = await get_and_check_file(audio_file, "audio/mpeg")
    audio_hash = calculate_sha1(audio_bytes)
    s3_uploads.append(
        {
            "path": f"{session.sonolus_id}/{chart_id}/{audio_hash}",
            "hash": audio_hash,
            "bytes": audio_bytes,
            "content-type": "audio/mpeg",
        }
    )

    if preview_file:
        preview_bytes = await get_and_check_file(preview_file, "audio/mpeg")
        preview_hash = calculate_sha1(preview_bytes)
        s3_uploads.append(
            {
                "path": f"{session.sonolus_id}/{chart_id}/{preview_hash}",
                "hash": preview_hash,
                "bytes": preview_bytes,
                "content-type": "audio/mpeg",
            }
        )

    if background_image:
        background_bytes = await get_and_check_file(background_image, "image/png")
        background_hash = calculate_sha1(background_bytes)
        s3_uploads.append(
            {
                "path": f"{session.sonolus_id}/{chart_id}/{background_hash}",
                "hash": background_hash,
                "bytes": background_bytes,
                "content-type": "image/png",
            }
        )
    async with app.s3_session_getter() as s3:
        bucket = await s3.Bucket(app.s3_bucket)
        tasks = []
        alr_added_hashes = []
        for file in s3_uploads:
            if file["hash"] in alr_added_hashes:
                continue
            alr_added_hashes.append(file["hash"])
            path = file["path"]
            file_bytes = file["bytes"]
            content_type = file["content-type"]
            task = bucket.upload_fileobj(
                Fileobj=io.BytesIO(file_bytes),
                Key=path,
                ExtraArgs={"ContentType": content_type},
            )
            tasks.append(task)
        await asyncio.gather(*tasks)
    query = charts.create_chart(
        chart=Chart(
            id=chart_id,
            author=session.sonolus_id,
            rating=data.rating,
            chart_author=data.author,
            title=data.title,
            artists=data.artists,
            jacket_file_hash=jacket_hash,
            music_file_hash=audio_hash,
            chart_file_hash=chart_hash,
            background_v1_file_hash=v1_hash,
            background_v3_file_hash=v3_hash,
            tags=data.tags or [],
            description=data.description,
            preview_file_hash=preview_hash if preview_file else None,
            background_file_hash=background_hash if background_image else None,
        )
    )
    query2 = accounts.update_cooldown(
        sonolus_id=session.sonolus_id,
        time_to_add=timedelta(minutes=1),  # XXX: 1 minute for now
    )

    async with app.db_acquire() as conn:
        result = await conn.fetchrow(query)
        if result:
            await conn.execute(query2)
            return {"id": result.id}
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Error while processing upload result.",
        )
