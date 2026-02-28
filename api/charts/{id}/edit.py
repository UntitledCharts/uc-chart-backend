import io, asyncio, gzip

from fastapi import APIRouter, Request, HTTPException, status, UploadFile, Form

from database import charts, leaderboards

import sonolus_converters

from helpers.models import ChartEditData
from helpers.hashing import calculate_sha1
from helpers.backgrounds import generate_backgrounds_resize_jacket
from helpers.constants import MAX_FILE_SIZES, MAX_TEXT_SIZES, MAX_RATINGS

from typing import Optional
from helpers.file_checks import get_and_check_file

from helpers.session import get_session, Session

from pydantic import ValidationError

from core import ChartFastAPI

router = APIRouter()


@router.patch("/")
async def main(
    request: Request,
    id: str,
    data: str = Form(...),
    jacket_image: Optional[UploadFile] = None,
    chart_file: Optional[UploadFile] = None,
    audio_file: Optional[UploadFile] = None,
    preview_file: Optional[UploadFile] = None,
    background_image: Optional[UploadFile] = None,
    session: Session = get_session(
        enforce_auth=True, enforce_type="external", allow_banned_users=False
    ),
):
    chart_updated = False

    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )

    app: ChartFastAPI = request.app
    try:
        data: ChartEditData = ChartEditData.model_validate_json(data)
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
        or (data.rating and data.rating > MAX_RATINGS["max"])
        or (data.rating and data.rating < MAX_RATINGS["min"])
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Length limits exceeded"
        )
    user = await session.user()
    query = charts.get_chart_by_id(id)
    async with app.db_acquire() as conn:
        result = await conn.fetchrow(query)
        if not result:
            raise HTTPException(status_code=404, detail="Chart not found.")
        old_chart_data = result
    if old_chart_data.author != user.sonolus_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="bro this aint your chart"
        )

    s3_uploads = []
    old_deletes = []

    if chart_file:
        if chart_file.size > MAX_FILE_SIZES["chart"]:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Uploaded files exceed file size limit.",
            )
        if data.includes_chart:
            result = sonolus_converters.detect((await chart_file.read()))
            await chart_file.seek(0)
            if not result:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid file format.",
                )
            chart_bytes = await chart_file.read()

            def convert() -> bytes:
                if result[0] == "sus":
                    converted = io.BytesIO()
                    score = sonolus_converters.sus.load(
                        io.TextIOWrapper(io.BytesIO(chart_bytes), encoding="utf-8")
                    )
                    sonolus_converters.next_sekai.export(converted, score)
                elif result[0] == "usc":
                    converted = io.BytesIO()
                    score = sonolus_converters.usc.load(
                        io.TextIOWrapper(io.BytesIO(chart_bytes), encoding="utf-8")
                    )
                    sonolus_converters.next_sekai.export(converted, score)
                elif result[0] == "lvd":
                    if not result[1].endswith("pysekai"):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Incorrect LevelData: {result[1]} (expected: pysekai)",
                        )
                    if not result[1].startswith("compress_"):
                        compressed_data = io.BytesIO()
                        with gzip.GzipFile(
                            fileobj=compressed_data,
                            mode="wb",
                            filename="LevelData",
                            mtime=0,
                        ) as f:
                            f.write(chart_bytes)
                        compressed_data.seek(0)
                        return compressed_data.getvalue()
                    return chart_bytes
                return converted.read()

            try:
                chart_bytes = await app.run_blocking(convert)
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
                )
            chart_hash = calculate_sha1(chart_bytes)
            if not chart_hash == old_chart_data.chart_file_hash:
                s3_uploads.append(
                    {
                        "path": f"{session.sonolus_id}/{id}/{chart_hash}",
                        "hash": chart_hash,
                        "bytes": chart_bytes,
                        "content-type": "application/gzip",
                    }
                )
                old_deletes.append("chart_file_hash")
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Includes unexpected file.",
            )

        chart_updated = True

    elif data.includes_chart:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="File not found."
        )
    if jacket_image:
        if jacket_image.size > MAX_FILE_SIZES["jacket"]:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Uploaded files exceed file size limit.",
            )
        if data.includes_jacket:
            jacket_bytes = await get_and_check_file(jacket_image, "image")
            jacket_hash = calculate_sha1(jacket_bytes)
            if not jacket_hash == old_chart_data.jacket_file_hash:
                old_deletes.append("jacket_file_hash")
                v1, v3, jacket_bytes = await app.run_blocking(
                    generate_backgrounds_resize_jacket, jacket_bytes
                )
                jacket_hash = calculate_sha1(jacket_bytes)
                s3_uploads.append(
                    {
                        "path": f"{session.sonolus_id}/{id}/{jacket_hash}",
                        "hash": jacket_hash,
                        "bytes": jacket_bytes,
                        "content-type": "image/png",
                    }
                )
                v1_hash = calculate_sha1(v1)
                s3_uploads.append(
                    {
                        "path": f"{session.sonolus_id}/{id}/{v1_hash}",
                        "hash": v1_hash,
                        "bytes": v1,
                        "content-type": "image/png",
                    }
                )
                v3_hash = calculate_sha1(v3)
                s3_uploads.append(
                    {
                        "path": f"{session.sonolus_id}/{id}/{v3_hash}",
                        "hash": v3_hash,
                        "bytes": v3,
                        "content-type": "image/png",
                    }
                )
                old_deletes.append("background_v1_file_hash")
                old_deletes.append("background_v3_file_hash")
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Includes unexpected file.",
            )
    elif data.includes_jacket:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="File not found."
        )
    if audio_file:
        if audio_file.size > MAX_FILE_SIZES["audio"]:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Uploaded files exceed file size limit.",
            )
        if data.includes_audio:
            audio_bytes = await get_and_check_file(audio_file, "audio/mpeg")
            audio_hash = calculate_sha1(audio_bytes)
            if not audio_hash == old_chart_data.music_file_hash:
                s3_uploads.append(
                    {
                        "path": f"{session.sonolus_id}/{id}/{audio_hash}",
                        "hash": audio_hash,
                        "bytes": audio_bytes,
                        "content-type": "audio/mpeg",
                    }
                )
                old_deletes.append("music_file_hash")
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Includes unexpected file.",
            )

        chart_updated = True
    elif data.includes_audio:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="File not found."
        )
    if preview_file:
        if preview_file.size > MAX_FILE_SIZES["preview"]:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Uploaded files exceed file size limit.",
            )
        if data.includes_preview and not data.delete_preview:
            preview_bytes = await get_and_check_file(preview_file, "audio/mpeg")
            preview_hash = calculate_sha1(preview_bytes)
            if not preview_hash == old_chart_data.preview_file_hash:
                s3_uploads.append(
                    {
                        "path": f"{session.sonolus_id}/{id}/{preview_hash}",
                        "hash": preview_hash,
                        "bytes": preview_bytes,
                        "content-type": "audio/mpeg",
                    }
                )
                old_deletes.append("preview_file_hash")
        elif data.delete_preview:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Can't delete and include.",
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Includes unexpected file.",
            )
    elif data.delete_preview and not data.includes_preview:
        old_deletes.append("preview_file_hash")
    elif data.includes_preview:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File not found.",
        )
    if background_image:
        if background_image.size > MAX_FILE_SIZES["background"]:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Uploaded files exceed file size limit.",
            )
        if data.includes_background and not data.delete_background:
            background_bytes = await get_and_check_file(background_image, "image/png")
            background_hash = calculate_sha1(background_bytes)
            if not background_hash == old_chart_data.background_file_hash:
                s3_uploads.append(
                    {
                        "path": f"{session.sonolus_id}/{id}/{background_hash}",
                        "hash": background_hash,
                        "bytes": background_bytes,
                        "content-type": "image/png",
                    }
                )
                old_deletes.append("background_file_hash")
        elif data.delete_background:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Can't delete and include.",
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Includes unexpected file.",
            )
    elif data.delete_background and not data.includes_background:
        old_deletes.append("background_file_hash")
    elif data.includes_background:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File not found.",
        )

    all_hash_keys = {
        "background_file_hash",
        "background_v1_file_hash",
        "background_v3_file_hash",
        "jacket_file_hash",
        "chart_file_hash",
        "music_file_hash",
        "preview_file_hash",
    }
    old_deletes_set = set(old_deletes)
    kept_hash_keys = all_hash_keys - old_deletes_set
    kept_hashes = set(
        getattr(old_chart_data, hash_key)
        for hash_key in kept_hash_keys
        if hash_key in old_chart_data.model_fields
    )
    deleted_candidate_hashes = set(
        getattr(old_chart_data, hash_key)
        for hash_key in old_deletes
        if hash_key in old_chart_data.model_fields
    )
    deleted_hashes = deleted_candidate_hashes - kept_hashes
    if deleted_hashes or s3_uploads:
        async with app.s3_session_getter() as s3:
            bucket = await s3.Bucket(app.s3_bucket)
            tasks = []
            alr_deleted_hashes = []
            for file_hash in deleted_hashes:
                if file_hash in alr_deleted_hashes:
                    continue
                key = f"{session.sonolus_id}/{id}/{file_hash}"
                obj = await bucket.Object(key)
                task = obj.delete()
                tasks.append(task)
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

            if chart_updated:
                prefix = f"{old_chart_data.author}/{old_chart_data.id}/replays/"
                objects = [obj async for obj in bucket.objects.filter(Prefix=prefix)]
                if objects:
                    tasks += [obj.delete() for obj in objects]

            await asyncio.gather(*tasks)
    query = charts.update_metadata(
        chart_id=id,
        chart_author=data.author,
        rating=data.rating,
        title=data.title,
        artists=data.artists,
        tags=data.tags or None,
        description=(
            data.description
            if (data.description and data.description.strip() != "")
            else None
        ),
        update_none_description=(
            False if (data.description and data.description.strip() != "") else True
        ),
    )
    query2 = charts.update_file_hash(
        chart_id=id,
        jacket_hash=jacket_hash if data.includes_jacket and jacket_image else None,
        v1_hash=v1_hash if data.includes_jacket and jacket_image else None,
        v3_hash=v3_hash if data.includes_jacket and jacket_image else None,
        music_hash=audio_hash if data.includes_audio and audio_file else None,
        chart_hash=chart_hash if data.includes_chart and chart_file else None,
        preview_hash=(preview_hash if data.includes_preview and preview_file else None),
        background_hash=(
            background_hash if data.includes_background and background_image else None
        ),
        confirm_change=True,
        update_none_preview=True if data.delete_preview else False,
        update_none_background=True if data.delete_background else False,
    )

    async with app.db_acquire() as conn:
        await conn.execute(query)
        await conn.execute(query2)

        if chart_updated:
            await conn.execute(leaderboards.delete_leaderboards(old_chart_data.id))
    return {"result": "success"}
