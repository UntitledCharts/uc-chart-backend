from core import ChartFastAPI

from fastapi import APIRouter, Request, HTTPException, status, UploadFile

from helpers.delete import delete_from_s3

from database import accounts, charts
from helpers.models import UserProfile, UpdateDescriptionRequest
from helpers.session import get_session, Session
from helpers.hashing import calculate_sha256
from helpers.constants import MAX_FILE_SIZES

from PIL import Image
import io

router = APIRouter()


@router.delete("/")
async def main_delete(request: Request, id: str):
    app: ChartFastAPI = request.app

    if request.headers.get(app.auth_header) != app.auth:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="why?")

    await delete_from_s3(app, id)

    query = accounts.delete_account(id, confirm_change=True)

    async with app.db_acquire() as conn:
        await conn.execute(query)

    return {"result": "success"}


@router.get("/")
async def get(request: Request, id: str):
    app: ChartFastAPI = request.app

    async with app.db_acquire() as conn:
        account = await conn.fetchrow(accounts.get_public_account(id))

        if not account:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

        _, chart_list_query = charts.get_chart_list(
            page=0, items_per_page=5, sort_by="likes", owned_by=account.sonolus_id
        )

        chart_list = await conn.fetch(chart_list_query)

    return UserProfile(
        account=account,
        charts=chart_list if chart_list else [],
        asset_base_url=app.s3_asset_base_url,
    )


@router.get("/stats/")
async def get(request: Request, id: str):
    app: ChartFastAPI = request.app

    async with app.db_acquire() as conn:
        account_stats = await conn.fetchrow(accounts.get_account_stats(id))

        if not account_stats:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return account_stats.model_dump()


# user stuff
@router.delete("/profile")
async def delete_profile_hash(
    request: Request,
    id: str,
    session: Session = get_session(enforce_auth=True, enforce_type="external"),
):
    app: ChartFastAPI = request.app

    user = await session.user()
    if user.sonolus_id != id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify another user's profile",
        )

    query = accounts.update_profile_hash(id, None)

    async with app.db_acquire() as conn:
        await conn.execute(query)

    return {"result": "success"}


@router.delete("/banner")
async def delete_banner_hash(
    request: Request,
    id: str,
    session: Session = get_session(enforce_auth=True, enforce_type="external"),
):
    app: ChartFastAPI = request.app

    user = await session.user()
    if user.sonolus_id != id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify another user's banner",
        )

    query = accounts.update_banner_hash(id, None)

    async with app.db_acquire() as conn:
        await conn.execute(query)

    return {"result": "success"}


@router.post("/description")
async def update_description(
    request: Request,
    id: str,
    data: UpdateDescriptionRequest,
    session: Session = get_session(enforce_auth=True, enforce_type=False),
):
    app: ChartFastAPI = request.app

    user = await session.user()
    if user.sonolus_id != id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify another user's description",
        )

    query = accounts.update_description(id, data.description)

    async with app.db_acquire() as conn:
        await conn.execute(query)

    return {"result": "success"}


@router.post("/profile/upload")
async def upload_profile(
    request: Request,
    id: str,
    file: UploadFile,
    session: Session = get_session(enforce_auth=True, enforce_type="external"),
):
    app: ChartFastAPI = request.app

    user = await session.user()
    if user.sonolus_id != id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify another user's profile",
        )

    # Read file content
    file_content = await file.read()

    # Check file size
    if len(file_content) > MAX_FILE_SIZES["account_pfp"]:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="File size exceeds limit",
        )

    # Convert to WebP
    try:
        image = Image.open(io.BytesIO(file_content))
        webp_buffer = io.BytesIO()
        image.save(webp_buffer, format="WEBP")
        webp_bytes = webp_buffer.getvalue()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid image file: {str(e)}",
        )

    # Calculate hash
    file_hash = calculate_sha256(webp_bytes)

    # Upload to S3
    async with app.s3_session_getter() as s3:
        bucket = await s3.Bucket(app.s3_bucket)
        path = f"{session.sonolus_id}/profile/{file_hash}"
        await bucket.upload_fileobj(
            Fileobj=io.BytesIO(webp_bytes),
            Key=path,
            ExtraArgs={"ContentType": "image/webp"},
        )

    # Update database
    query = accounts.update_profile_hash(id, file_hash)
    async with app.db_acquire() as conn:
        await conn.execute(query)

    return {"result": "success", "hash": file_hash}


@router.post("/banner/upload")
async def upload_banner(
    request: Request,
    id: str,
    file: UploadFile,
    session: Session = get_session(enforce_auth=True, enforce_type="external"),
):
    app: ChartFastAPI = request.app

    user = await session.user()
    if user.sonolus_id != id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify another user's banner",
        )

    # Read file content
    file_content = await file.read()

    # Check file size
    if len(file_content) > MAX_FILE_SIZES["account_banner"]:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="File size exceeds limit",
        )

    # Convert to WebP
    try:
        image = Image.open(io.BytesIO(file_content))
        webp_buffer = io.BytesIO()
        image.save(webp_buffer, format="WEBP")
        webp_bytes = webp_buffer.getvalue()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid image file: {str(e)}",
        )

    # Calculate hash
    file_hash = calculate_sha256(webp_bytes)

    # Upload to S3
    async with app.s3_session_getter() as s3:
        bucket = await s3.Bucket(app.s3_bucket)
        path = f"{session.sonolus_id}/banner/{file_hash}"
        await bucket.upload_fileobj(
            Fileobj=io.BytesIO(webp_bytes),
            Key=path,
            ExtraArgs={"ContentType": "image/webp"},
        )

    # Update database
    query = accounts.update_banner_hash(id, file_hash)
    async with app.db_acquire() as conn:
        await conn.execute(query)

    return {"result": "success", "hash": file_hash}
