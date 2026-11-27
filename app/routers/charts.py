import asyncio
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from pydantic import Field, ValidationError

from app.auth.principal import Principal, auth
from app.context import AppContext, get_context
from app.database import accounts as accounts_db
from app.database import charts as charts_db
from app.database import leaderboards as leaderboards_db
from app.database.charts import ChartFilters, SortBy
from app.errors import ApiError, bad_request, forbidden, not_found
from app.models import present, schemas
from app.services import audio, chart_files, conversion_runner, purge, staff_log
from app.services.chart_access import (
    ChartAccess,
    check_chart_id,
    load_chart,
    require_outranks_owner,
)
from app.services.uploads import read_limited

router = APIRouter(prefix="/v1/charts", tags=["charts"])

ListStatus = Literal["PUBLIC", "PUBLIC_MINE", "UNLISTED", "PRIVATE", "ALL"]


class ChartMetadata(schemas.Schema):
    """Chart metadata sent with an upload.

    Attributes:
        rating: Difficulty rating.
        title: Title.
        author: Free-text charter name.
        artists: Artists.
        tags: Tags.
        description: Description.
        includes_background: Ignored, accepted for compatibility.
        includes_preview: Ignored, accepted for compatibility.
    """

    rating: int
    title: str
    author: str
    artists: str
    tags: list[str] | None = None
    description: str | None = None
    includes_background: bool = False
    includes_preview: bool = False


class ChartEdit(schemas.Schema):
    """Changes sent with an edit; only fields present are changed.

    Attributes:
        author: Free-text charter name.
        rating: Difficulty rating.
        title: Title.
        artists: Artists.
        description: Description, None to clear it.
        tags: Tags, empty to clear them.
        includes_chart: A new chart file is attached.
        includes_jacket: A new jacket is attached.
        includes_audio: New audio is attached.
        includes_preview: A new preview is attached.
        includes_background: A new background is attached.
        delete_preview: Remove the preview.
        delete_background: Remove the background.
    """

    author: str | None = None
    rating: int | None = None
    title: str | None = None
    artists: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    includes_chart: bool = False
    includes_jacket: bool = False
    includes_audio: bool = False
    includes_preview: bool = False
    includes_background: bool = False
    delete_preview: bool = False
    delete_background: bool = False


class VisibilityBody(schemas.Schema):
    """New visibility.

    Attributes:
        status: Visibility.
    """

    status: Literal["PUBLIC", "UNLISTED", "PRIVATE"]


class VisibilityChange(schemas.Schema):
    """Result of a visibility change.

    Attributes:
        previous_status: Status before.
        status: Status after.
        first_publish: Whether the chart became public for the first time.
    """

    previous_status: str
    status: str
    first_publish: bool


class ScheduleBody(schemas.Schema):
    """Scheduled publish time.

    Attributes:
        publish_at: When to publish the chart.
    """

    publish_at: datetime


class StaffPickBody(schemas.Schema):
    """Staff pick flag.

    Attributes:
        value: Whether the chart is staff picked.
    """

    value: bool


class RatingBody(schemas.Schema):
    """New rating.

    Attributes:
        rating: Difficulty rating.
    """

    rating: Decimal = Field(allow_inf_nan=False)


def viewer_of(access: ChartAccess) -> schemas.ChartViewer:
    """Describe the viewer's permissions on a chart.

    Args:
        access: Chart access.

    Returns:
        schemas.ChartViewer: The viewer permissions.
    """
    return schemas.ChartViewer(
        is_owner=access.is_owner,
        chart_permissions=sorted(access.chart_permissions),
        staff_permissions=access.staff_permissions(),
    )


def details(context: AppContext, access: ChartAccess) -> schemas.ChartDetails:
    """Build chart details for the viewer.

    Args:
        context: App context.
        access: Chart access.

    Returns:
        schemas.ChartDetails: The details.
    """
    base = present.chart(
        access.chart, context.storage, show_deleted=access.can_view_hidden or access.is_owner
    )
    return schemas.ChartDetails(**base.model_dump(), viewer=viewer_of(access))


def check_text(context: AppContext, values: dict[str, str | None], tags: list[str] | None) -> None:
    """Enforce text length limits.

    Args:
        context: App context.
        values: Text fields keyed by limit name.
        tags: Tags, if given.
    """
    limits = context.constants.text
    for field, value in values.items():
        if value is not None and len(value) > limits.max_lengths[field]:
            raise bad_request("error.text.too_long", field=field, limit=limits.max_lengths[field])
    if tags is not None:
        if len(tags) > limits.max_tags:
            raise bad_request("error.text.too_many_tags", limit=limits.max_tags)
        for tag in tags:
            if len(tag) > limits.max_lengths["tag"]:
                raise bad_request(
                    "error.text.too_long", field="tag", limit=limits.max_lengths["tag"]
                )


def check_rating(context: AppContext, rating: Decimal | int) -> None:
    """Enforce rating bounds and precision.

    Args:
        context: App context.
        rating: Rating to check.
    """
    limits = context.constants.rating
    value = Decimal(rating)
    if value < limits.min or value > limits.max:
        raise bad_request("error.chart.rating_out_of_range", min=limits.min, max=limits.max)
    exponent = value.normalize().as_tuple().exponent
    if isinstance(exponent, int) and -exponent > limits.decimal_places:
        raise bad_request("error.chart.rating_precision", decimal_places=limits.decimal_places)


def parse_json_form(model: type[schemas.Schema], data: str) -> schemas.Schema:
    """Validate the JSON ``data`` form field.

    Args:
        model: Model to validate into.
        data: Field value.

    Returns:
        schemas.Schema: The parsed model.
    """
    try:
        return model.model_validate_json(data)
    except ValidationError as error:
        fields = [
            {"location": [str(part) for part in item["loc"]], "type": item["type"]}
            for item in error.errors()
        ]
        raise ApiError(422, "error.request.invalid", {"fields": fields}) from None


async def read_upload(context: AppContext, upload: UploadFile, kind: str, field: str) -> bytes:
    """Read an uploaded chart file and check its magic bytes.

    Args:
        context: App context.
        upload: Uploaded file.
        kind: Size limit and type key: jacket, chart, audio, preview or background.
        field: Form field name.

    Returns:
        bytes: File contents.
    """
    data = await read_limited(upload, context.constants.files.max_sizes[kind], field)
    if kind == "jacket" and not chart_files.is_supported_image(data):
        raise bad_request("error.file.invalid_image_type", field=field)
    if kind in ("audio", "preview") and not chart_files.is_mp3(data):
        raise bad_request("error.file.invalid_audio_type", field=field)
    if kind == "background" and not chart_files.is_png(data):
        raise bad_request("error.file.invalid_png", field=field)
    return data


async def convert(context: AppContext, request: Request, raw: bytes) -> bytes:
    """Convert an uploaded chart in a child process, giving up when the client disconnects.

    Args:
        context: App context.
        request: Current request.
        raw: Uploaded chart file.

    Returns:
        bytes: Gzipped next_sekai level data.
    """
    return await conversion_runner.convert_chart(context, raw, request.is_disconnected)


async def cancel_all(*tasks: asyncio.Future[Any]) -> None:
    """Cancel tasks and wait until each one has stopped.

    Args:
        *tasks: Tasks to cancel.
    """
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def process_jacket(context: AppContext, data: bytes) -> chart_files.JacketImages:
    """Process a jacket off the event loop.

    Args:
        context: App context.
        data: Uploaded jacket.

    Returns:
        chart_files.JacketImages: The processed images.
    """
    images = context.constants.images
    return await context.run_blocking(
        chart_files.process_jacket, data, images.jacket_size, images.max_pixels
    )


async def process_audio(context: AppContext, data: bytes) -> bytes:
    """Check and re-encode uploaded audio off the event loop, limited per worker.

    Args:
        context: App context.
        data: Uploaded MP3.

    Returns:
        bytes: Constant bitrate MP3.
    """
    async with context.audio_jobs:
        return await context.run_blocking(audio.ensure_cbr_mp3, data, context.constants.audio)


async def reserve_cooldown(context: AppContext, sonolus_id: str) -> None:
    """Start the account's upload cooldown, failing with 429 while one is running.

    Args:
        context: App context.
        sonolus_id: Uploading account id.
    """
    async with context.database.connection() as connection:
        busy_until = await accounts_db.reserve_upload_cooldown(
            connection, sonolus_id, context.constants.charts.upload_cooldown_seconds
        )
    if busy_until is not None and not context.config.server.debug:
        remaining = max(0, int((busy_until - datetime.now(timezone.utc)).total_seconds()))
        raise ApiError(429, "error.chart.upload_cooldown", {"seconds_remaining": remaining})


async def release_cooldown(context: AppContext, sonolus_id: str) -> None:
    """Cancel the account's upload cooldown after a failed upload.

    Args:
        context: App context.
        sonolus_id: Uploading account id.
    """
    async with context.database.connection() as connection:
        await accounts_db.release_upload_cooldown(connection, sonolus_id)


def as_utc(value: datetime) -> datetime:
    """Treat naive times as UTC.

    Args:
        value: A time.

    Returns:
        datetime: An aware time.
    """
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@router.get("", response_model=schemas.Page[schemas.Chart])
async def list_charts(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
    page: Annotated[int, Query(ge=0, le=1000000)] = 0,
    status: ListStatus = "PUBLIC",
    staff_pick: bool | None = None,
    min_rating: int | None = None,
    max_rating: int | None = None,
    tags: Annotated[list[str] | None, Query()] = None,
    min_likes: int | None = None,
    max_likes: int | None = None,
    min_comments: int | None = None,
    max_comments: int | None = None,
    liked_by: bool = False,
    commented_on: bool = False,
    title_includes: str | None = None,
    description_includes: str | None = None,
    artists_includes: str | None = None,
    author_includes: str | None = None,
    meta_includes: str | None = None,
    owner_handle: int | None = None,
    sort_by: SortBy = "created_at",
    sort_order: Literal["desc", "asc"] = "desc",
) -> schemas.Page[schemas.Chart]:
    """Search and list charts.

    Args:
        context: App context.
        principal: The caller.
        page: Zero-based page.
        status: PUBLIC, or the caller's own PUBLIC_MINE, UNLISTED, PRIVATE or ALL charts.
        staff_pick: Only picked or unpicked charts.
        min_rating: Lowest rating level.
        max_rating: Highest rating level.
        tags: Charts must have all of these tags.
        min_likes: Fewest likes.
        max_likes: Most likes.
        min_comments: Fewest comments.
        max_comments: Most comments.
        liked_by: Only charts the caller liked.
        commented_on: Only charts the caller commented on.
        title_includes: Title substring.
        description_includes: Description substring.
        artists_includes: Artists substring.
        author_includes: Substring of ``charter#handle``.
        meta_includes: Substring of title, description, artists or charter.
        owner_handle: Only charts of the owner with this handle.
        sort_by: Sort key.
        sort_order: Sort direction.

    Returns:
        schemas.Page[schemas.Chart]: The page.
    """
    filters = ChartFilters(
        status=None if status == "ALL" else ("PUBLIC" if status == "PUBLIC_MINE" else status),
        staff_pick=staff_pick,
        min_rating=min_rating,
        max_rating=max_rating,
        tags=tags or [],
        min_likes=min_likes,
        max_likes=max_likes,
        min_comments=min_comments,
        max_comments=max_comments,
        title_includes=title_includes,
        description_includes=description_includes,
        artists_includes=artists_includes,
        author_includes=author_includes,
        meta_includes=meta_includes,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    if status != "PUBLIC":
        account = principal.require_account()
        if status != "PUBLIC_MINE":
            principal.require_scope("chart:read")
        if owner_handle is not None:
            raise bad_request("error.chart.own_list_with_owner")
        filters.owned_by = account.sonolus_id
    elif owner_handle is not None:
        filters.owner_handle = owner_handle
    if liked_by or commented_on:
        account = principal.require_account()
        principal.require_scope("user:read")
        filters.liked_by = account.sonolus_id if liked_by else None
        filters.commented_by = account.sonolus_id if commented_on else None
    page_size = context.constants.charts.page_size
    async with context.database.connection() as connection:
        rows, total = await charts_db.list_charts(
            connection, filters, page, page_size, principal.sonolus_id
        )
    return schemas.Page[schemas.Chart](
        items=[present.chart(row, context.storage) for row in rows],
        page=page,
        page_count=present.page_count(total, page_size),
    )


@router.get("/random", response_model=list[schemas.Chart])
async def random_charts(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
    staff_pick: bool | None = None,
) -> list[schemas.Chart]:
    """Pick random public charts.

    Args:
        context: App context.
        principal: The caller.
        staff_pick: Only picked or unpicked charts.

    Returns:
        list[schemas.Chart]: The charts.
    """
    async with context.database.connection() as connection:
        rows = await charts_db.random_charts(
            connection, context.constants.charts.random_count, principal.sonolus_id, staff_pick
        )
    return [present.chart(row, context.storage) for row in rows]


@router.post("", status_code=201, response_model=schemas.Chart)
async def upload_chart(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal, Depends(auth(account=True, scope="chart:upload", banned=False))
    ],
    request: Request,
    data: Annotated[str, Form()],
    jacket_image: Annotated[UploadFile, File()],
    chart_file: Annotated[UploadFile, File()],
    audio_file: Annotated[UploadFile, File()],
    preview_file: Annotated[UploadFile | None, File()] = None,
    background_image: Annotated[UploadFile | None, File()] = None,
) -> schemas.Chart:
    """Upload a new private chart.

    Args:
        context: App context.
        principal: The caller.
        request: Current request.
        data: JSON chart metadata.
        jacket_image: Jacket image.
        chart_file: Chart file in any supported format.
        audio_file: MP3 audio.
        preview_file: MP3 preview.
        background_image: PNG background.

    Returns:
        schemas.Chart: The new chart.
    """
    account = principal.require_account()
    metadata = parse_json_form(ChartMetadata, data)
    assert isinstance(metadata, ChartMetadata)
    check_text(
        context,
        {
            "description": metadata.description,
            "artists": metadata.artists,
            "title": metadata.title,
            "author": metadata.author,
        },
        metadata.tags,
    )
    check_rating(context, metadata.rating)
    jacket_raw = await read_upload(context, jacket_image, "jacket", "jacket_image")
    chart_raw = await read_upload(context, chart_file, "chart", "chart_file")
    audio_raw = await read_upload(context, audio_file, "audio", "audio_file")
    preview_raw = (
        await read_upload(context, preview_file, "preview", "preview_file")
        if preview_file
        else None
    )
    background_raw = (
        await read_upload(context, background_image, "background", "background_image")
        if background_image
        else None
    )
    await reserve_cooldown(context, account.sonolus_id)
    chart_id = uuid.uuid4().hex
    uploaded: list[str] = []
    try:
        conversion = asyncio.ensure_future(convert(context, request, chart_raw))
        jacket_job = asyncio.ensure_future(process_jacket(context, jacket_raw))
        audio_job = asyncio.ensure_future(process_audio(context, audio_raw))
        try:
            level_data, jacket, music = await asyncio.gather(conversion, jacket_job, audio_job)
        except BaseException:
            await cancel_all(conversion, jacket_job, audio_job)
            raise
        files = {
            "jacket_file_hash": (jacket.jacket, "image/png"),
            "background_v1_file_hash": (jacket.background_v1, "image/png"),
            "background_v3_file_hash": (jacket.background_v3, "image/png"),
            "chart_file_hash": (level_data, "application/gzip"),
            "music_file_hash": (music, "audio/mpeg"),
        }
        if preview_raw is not None:
            files["preview_file_hash"] = (preview_raw, "audio/mpeg")
        if background_raw is not None:
            files["background_file_hash"] = (background_raw, "image/png")
        hashes = {column: chart_files.sha1(content) for column, (content, _) in files.items()}
        prefix = f"{account.sonolus_id}/{chart_id}/"
        unique = {
            hashes[column]: (content, content_type)
            for column, (content, content_type) in files.items()
        }
        await context.storage.put_many(
            [
                (prefix + file_hash, content, content_type)
                for file_hash, (content, content_type) in unique.items()
            ]
        )
        uploaded = [prefix + file_hash for file_hash in unique]
        async with context.database.connection() as connection:
            await charts_db.create_chart(
                connection,
                {
                    "id": chart_id,
                    "author": account.sonolus_id,
                    "rating": Decimal(metadata.rating),
                    "description": metadata.description,
                    "chart_author": metadata.author,
                    "title": metadata.title,
                    "artists": metadata.artists,
                    "tags": metadata.tags or [],
                    **hashes,
                },
            )
            row = await charts_db.get_chart(connection, chart_id, account.sonolus_id)
    except BaseException:
        await release_cooldown(context, account.sonolus_id)
        if uploaded:
            await context.storage.discard_keys(uploaded)
        raise
    assert row is not None
    context.discord.new_upload(
        present.discord_chart(row, context.storage),
        f"{account.sonolus_username}#{account.sonolus_handle}",
    )
    return present.chart(row, context.storage, show_deleted=True)


@router.get("/{chart_id}", response_model=schemas.ChartDetails)
async def get_chart(
    chart_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
    is_preview: bool = False,
) -> schemas.ChartDetails:
    """Return a chart with the viewer's permissions.

    Args:
        chart_id: Chart id.
        context: App context.
        principal: The caller.
        is_preview: Don't count this as opening the chart.

    Returns:
        schemas.ChartDetails: The chart.
    """
    async with context.database.connection() as connection:
        access = await load_chart(connection, principal, chart_id)
        if (
            principal.account is not None
            and not principal.is_oauth
            and not is_preview
            and not access.chart.hidden
        ):
            await accounts_db.record_opened_level(
                connection,
                principal.account.sonolus_id,
                int(round(access.chart.rating)),
                context.constants.charts.recent_opened_levels_kept,
            )
    return details(context, access)


@router.patch("/{chart_id}", response_model=schemas.ChartDetails)
async def edit_chart(
    chart_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, banned=False))],
    request: Request,
    data: Annotated[str, Form()],
    jacket_image: Annotated[UploadFile | None, File()] = None,
    chart_file: Annotated[UploadFile | None, File()] = None,
    audio_file: Annotated[UploadFile | None, File()] = None,
    preview_file: Annotated[UploadFile | None, File()] = None,
    background_image: Annotated[UploadFile | None, File()] = None,
) -> schemas.ChartDetails:
    """Edit a chart's metadata and files.

    Args:
        chart_id: Chart id.
        context: App context.
        principal: The caller.
        request: Current request.
        data: JSON changes; only fields present are changed.
        jacket_image: New jacket.
        chart_file: New chart file.
        audio_file: New MP3 audio.
        preview_file: New MP3 preview.
        background_image: New PNG background.

    Returns:
        schemas.ChartDetails: The edited chart.
    """
    edit = parse_json_form(ChartEdit, data)
    assert isinstance(edit, ChartEdit)
    sent = edit.model_fields_set
    metadata_fields = {"author", "rating", "title", "artists", "description", "tags"} & sent
    file_flags = {
        "chart": (edit.includes_chart, chart_file),
        "jacket": (edit.includes_jacket, jacket_image),
        "audio": (edit.includes_audio, audio_file),
        "preview": (edit.includes_preview, preview_file),
        "background": (edit.includes_background, background_image),
    }
    for name, (included, upload) in file_flags.items():
        if upload is not None and not included:
            raise bad_request("error.chart.unexpected_file", file=name)
        if included and upload is None:
            raise bad_request("error.chart.missing_file", file=name)
    if (edit.delete_preview and edit.includes_preview) or (
        edit.delete_background and edit.includes_background
    ):
        raise bad_request("error.chart.delete_and_include")
    changes_files = (
        any(included for included, _ in file_flags.values())
        or edit.delete_preview
        or edit.delete_background
    )
    if not metadata_fields and not changes_files:
        raise bad_request("error.chart.nothing_to_edit")
    if metadata_fields:
        principal.require_scope("chart:metadata")
    if changes_files:
        principal.require_scope("chart:edit")
    check_text(
        context,
        {
            "description": edit.description if "description" in sent else None,
            "artists": edit.artists,
            "title": edit.title,
            "author": edit.author,
        },
        edit.tags if "tags" in sent else None,
    )
    if "rating" in sent and edit.rating is not None:
        check_rating(context, edit.rating)
    for required in ("author", "title", "artists", "rating"):
        if required in sent and getattr(edit, required) is None:
            raise bad_request("error.chart.field_required", field=required)

    async with context.database.connection() as connection:
        access = await load_chart(connection, principal, chart_id)
    if access.chart.deleted_at is not None:
        raise not_found("error.chart.not_found")
    if metadata_fields and not access.has_chart_permission("edit.metadata"):
        raise forbidden("error.chart.permission_required", permission="edit.metadata")
    if changes_files and not access.has_chart_permission("edit.files"):
        raise forbidden("error.chart.permission_required", permission="edit.files")
    chart = access.chart

    reads = {
        "jacket": (jacket_image, "jacket_image"),
        "chart": (chart_file, "chart_file"),
        "audio": (audio_file, "audio_file"),
        "preview": (preview_file, "preview_file"),
        "background": (background_image, "background_image"),
    }
    raw_files: dict[str, bytes] = {}
    for kind, (upload, field) in reads.items():
        if upload is not None:
            raw_files[kind] = await read_upload(context, upload, kind, field)

    editor_id = principal.require_account().sonolus_id
    if raw_files:
        await reserve_cooldown(context, editor_id)
    current = {column: getattr(chart, column) for column in charts_db.FILE_COLUMNS}
    prefix = f"{chart.author}/{chart.id}/"
    uploaded: list[str] = []
    try:
        new_files: dict[str, tuple[bytes, str]] = {}
        if "chart" in raw_files:
            new_files["chart_file_hash"] = (
                await convert(context, request, raw_files["chart"]),
                "application/gzip",
            )
        if "jacket" in raw_files:
            jacket = await process_jacket(context, raw_files["jacket"])
            new_files["jacket_file_hash"] = (jacket.jacket, "image/png")
            new_files["background_v1_file_hash"] = (jacket.background_v1, "image/png")
            new_files["background_v3_file_hash"] = (jacket.background_v3, "image/png")
        if "audio" in raw_files:
            new_files["music_file_hash"] = (
                await process_audio(context, raw_files["audio"]),
                "audio/mpeg",
            )
        if "preview" in raw_files:
            new_files["preview_file_hash"] = (raw_files["preview"], "audio/mpeg")
        if "background" in raw_files:
            new_files["background_file_hash"] = (raw_files["background"], "image/png")

        new_hashes = {
            column: chart_files.sha1(content) for column, (content, _) in new_files.items()
        }
        if edit.delete_preview:
            new_hashes["preview_file_hash"] = None
        if edit.delete_background:
            new_hashes["background_file_hash"] = None
        after = {**current, **new_hashes}
        to_upload = {
            new_hashes[column]: new_files[column]
            for column in new_files
            if new_hashes[column] != current.get(column)
        }
        await context.storage.put_many(
            [
                (prefix + file_hash, content, content_type)
                for file_hash, (content, content_type) in to_upload.items()
            ]
        )
        uploaded = [
            prefix + file_hash for file_hash in to_upload if file_hash not in current.values()
        ]

        values: dict[str, object] = {}
        if "author" in sent:
            values["chart_author"] = edit.author
        if "title" in sent:
            values["title"] = edit.title
        if "artists" in sent:
            values["artists"] = edit.artists
        if "rating" in sent:
            values["rating"] = Decimal(edit.rating or 0)
        if "description" in sent:
            values["description"] = (
                edit.description if edit.description and edit.description.strip() else None
            )
        if "tags" in sent:
            values["tags"] = edit.tags or []
        values.update(
            {column: value for column, value in new_hashes.items() if value != current.get(column)}
        )
        gameplay_changed = any(
            new_hashes.get(column, current[column]) != current[column]
            for column in ("chart_file_hash", "music_file_hash")
        )
        async with context.database.transaction() as connection:
            await charts_db.update_chart(connection, chart.id, values)
            if gameplay_changed:
                await leaderboards_db.delete_chart_records(connection, chart.id)
    except BaseException:
        if raw_files:
            await release_cooldown(context, editor_id)
        if uploaded:
            await context.storage.discard_keys(uploaded)
        raise
    still_used = {value for value in after.values() if value}
    stale = [prefix + value for value in current.values() if value and value not in still_used]
    await context.storage.discard_keys(stale)
    if gameplay_changed:
        await context.storage.discard_prefix(prefix + "replays/")
    async with context.database.connection() as connection:
        refreshed = await load_chart(connection, principal, chart_id)
    return details(context, refreshed)


@router.delete("/{chart_id}", status_code=204)
async def delete_chart(
    chart_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(scope="chart:delete", banned=False))],
    instant: bool = False,
) -> Response:
    """Delete a chart; it is purged after the grace period unless restored.

    Args:
        chart_id: Chart id.
        context: App context.
        principal: The caller.
        instant: Purge immediately; admin key only.

    Returns:
        Response: Empty response.
    """
    if instant and not principal.is_admin_key:
        raise forbidden("error.auth.admin_key_required")
    async with context.database.connection() as connection:
        access = await load_chart(connection, principal, chart_id)
        if not access.is_owner:
            principal.require_permission("chart.delete")
            await require_outranks_owner(connection, access)
    if instant:
        if not await purge.purge_chart(context, chart_id):
            raise not_found("error.chart.not_found")
        async with context.database.connection() as connection:
            await staff_log.record(
                context,
                connection,
                principal,
                "chart_purge",
                "chart",
                chart_id,
                json.dumps({"title": access.chart.title, "owner": access.chart.author}),
                None,
            )
        return Response(status_code=204)
    async with context.database.transaction() as connection:
        deleted_at = await charts_db.soft_delete(connection, chart_id)
        if deleted_at is not None and not access.is_owner:
            await staff_log.record(
                context, connection, principal, "delete", "chart", chart_id, None, "soft_deleted"
            )
    return Response(status_code=204)


@router.post("/{chart_id}/restore", status_code=204)
async def restore_chart(
    chart_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False, banned=False))],
) -> Response:
    """Restore a deleted chart.

    Args:
        chart_id: Chart id.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    principal.require_permission("chart.restore")
    async with context.database.transaction() as connection:
        access = await load_chart(connection, principal, chart_id, allow_hidden=True)
        await require_outranks_owner(connection, access)
        if not await charts_db.restore(connection, chart_id):
            raise not_found("error.chart.not_deleted")
        await staff_log.record(
            context, connection, principal, "undelete", "chart", chart_id, "soft_deleted", None
        )
    return Response(status_code=204)


async def _visibility_access(
    context: AppContext, principal: Principal, chart_id: str
) -> ChartAccess:
    """Load a chart the caller may change the visibility or schedule of.

    Args:
        context: App context.
        principal: The caller.
        chart_id: Chart id.

    Returns:
        ChartAccess: The chart access.
    """
    async with context.database.connection() as connection:
        access = await load_chart(connection, principal, chart_id)
        if not access.has_chart_permission("visibility"):
            principal.require_permission("chart.visibility")
            await require_outranks_owner(connection, access)
    return access


def _acting_as_staff(access: ChartAccess) -> bool:
    """Whether the caller changes the chart through staff power.

    Args:
        access: Chart access.

    Returns:
        bool: True when not owner or a collaborator with the visibility permission.
    """
    return not access.has_chart_permission("visibility")


@router.put("/{chart_id}/visibility", response_model=VisibilityChange)
async def set_visibility(
    chart_id: str,
    body: VisibilityBody,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(scope="chart:visibility", banned=False))],
) -> VisibilityChange:
    """Change a chart's visibility.

    Args:
        chart_id: Chart id.
        body: New visibility.
        context: App context.
        principal: The caller.

    Returns:
        VisibilityChange: What changed.
    """
    access = await _visibility_access(context, principal, chart_id)
    if access.chart.deleted_at is not None and not principal.has_permission("chart.visibility"):
        raise not_found("error.chart.not_found")
    async with context.database.transaction() as connection:
        change = await charts_db.set_status(connection, chart_id, body.status)
        if change is None:
            raise not_found("error.chart.not_found")
        if _acting_as_staff(access):
            await staff_log.record(
                context,
                connection,
                principal,
                "visibility_change",
                "chart",
                chart_id,
                change.previous_status,
                change.status,
            )
    chart_summary = present.discord_chart(access.chart, context.storage)
    context.discord.visibility_changed(
        chart_summary, change.previous_status, change.status, staff_log.actor_label(principal)
    )
    if change.first_publish:
        context.discord.first_publish(chart_summary)
    return VisibilityChange(
        previous_status=change.previous_status,
        status=change.status,
        first_publish=change.first_publish,
    )


@router.get("/{chart_id}/schedule", response_model=schemas.ScheduledChart)
async def get_schedule(
    chart_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
) -> schemas.ScheduledChart:
    """Return public data of a chart waiting to be published.

    Args:
        chart_id: Chart id.
        context: App context.
        principal: The caller.

    Returns:
        schemas.ScheduledChart: The chart.
    """
    check_chart_id(chart_id)
    async with context.database.connection() as connection:
        chart = await charts_db.get_chart(connection, chart_id)
    if chart is None or chart.scheduled_publish is None or chart.hidden:
        raise not_found("error.chart.not_scheduled")
    files = present.chart_files(chart, context.storage)
    return schemas.ScheduledChart(
        id=chart.id,
        title=chart.title,
        artists=chart.artists,
        charter=chart.charter,
        owner=schemas.AccountSummary(
            id=chart.author, handle=chart.author_handle, username=chart.author_username
        ),
        rating=chart.rating,
        jacket=files.jacket,
        background=files.background,
        background_v3=files.background_v3,
        scheduled_publish=chart.scheduled_publish,
    )


@router.put("/{chart_id}/schedule", status_code=204)
async def set_schedule(
    chart_id: str,
    body: ScheduleBody,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(scope="chart:visibility", banned=False))],
) -> Response:
    """Schedule a chart to become public.

    Args:
        chart_id: Chart id.
        body: Publish time.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    access = await _visibility_access(context, principal, chart_id)
    if access.chart.status == "PUBLIC":
        raise bad_request("error.chart.already_public")
    publish_at = as_utc(body.publish_at)
    async with context.database.transaction() as connection:
        _, previous = await charts_db.set_scheduled_publish(connection, chart_id, publish_at)
        if _acting_as_staff(access):
            await staff_log.record(
                context,
                connection,
                principal,
                "schedule_change",
                "chart",
                chart_id,
                previous.isoformat() if previous else None,
                publish_at.isoformat(),
            )
    context.discord.schedule_changed(
        present.discord_chart(access.chart, context.storage),
        int(publish_at.timestamp()),
        staff_log.actor_label(principal),
    )
    return Response(status_code=204)


@router.delete("/{chart_id}/schedule", status_code=204)
async def cancel_schedule(
    chart_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(scope="chart:visibility", banned=False))],
) -> Response:
    """Cancel a chart's scheduled publish.

    Args:
        chart_id: Chart id.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    access = await _visibility_access(context, principal, chart_id)
    if access.chart.scheduled_publish is None:
        raise not_found("error.chart.not_scheduled")
    async with context.database.transaction() as connection:
        _, previous = await charts_db.set_scheduled_publish(connection, chart_id, None)
        if _acting_as_staff(access):
            await staff_log.record(
                context,
                connection,
                principal,
                "schedule_change",
                "chart",
                chart_id,
                previous.isoformat() if previous else None,
                None,
            )
    context.discord.schedule_changed(
        present.discord_chart(access.chart, context.storage), None, staff_log.actor_label(principal)
    )
    return Response(status_code=204)


@router.put("/{chart_id}/staff-pick", status_code=204)
async def set_staff_pick(
    chart_id: str,
    body: StaffPickBody,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False, banned=False))],
) -> Response:
    """Staff pick or unpick a chart.

    Args:
        chart_id: Chart id.
        body: New flag.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    principal.require_permission("chart.staff_pick")
    async with context.database.transaction() as connection:
        access = await load_chart(connection, principal, chart_id)
        await require_outranks_owner(connection, access)
        previous = await charts_db.set_staff_pick(connection, chart_id, body.value)
        if previous is None:
            raise not_found("error.chart.not_found")
        await staff_log.record(
            context,
            connection,
            principal,
            "staff_pick",
            "chart",
            chart_id,
            str(previous),
            str(body.value),
        )
    if body.value and not previous:
        context.discord.staff_pick(present.discord_chart(access.chart, context.storage))
    return Response(status_code=204)


@router.put("/{chart_id}/rating", status_code=204)
async def set_rating(
    chart_id: str,
    body: RatingBody,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(oauth=False, banned=False))],
) -> Response:
    """Change a chart's rating as staff.

    Args:
        chart_id: Chart id.
        body: New rating.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    principal.require_permission("chart.rerate")
    try:
        check_rating(context, body.rating)
    except InvalidOperation:
        raise bad_request("error.chart.rating_out_of_range") from None
    async with context.database.transaction() as connection:
        access = await load_chart(connection, principal, chart_id, allow_hidden=True)
        await require_outranks_owner(connection, access)
        previous = await charts_db.set_rating(connection, chart_id, body.rating)
        if previous is None:
            raise not_found("error.chart.not_found")
        await staff_log.record(
            context,
            connection,
            principal,
            "constant_rerate",
            "chart",
            chart_id,
            str(previous),
            str(body.rating),
        )
    return Response(status_code=204)


@router.put("/{chart_id}/like", status_code=204)
async def like_chart(
    chart_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="chart:like", banned=False))],
) -> Response:
    """Like a chart.

    Args:
        chart_id: Chart id.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        await load_chart(connection, principal, chart_id)
        await charts_db.add_like(connection, chart_id, account.sonolus_id)
    return Response(status_code=204)


@router.delete("/{chart_id}/like", status_code=204)
async def unlike_chart(
    chart_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth(account=True, scope="chart:like", banned=False))],
) -> Response:
    """Remove a like from a chart.

    Args:
        chart_id: Chart id.
        context: App context.
        principal: The caller.

    Returns:
        Response: Empty response.
    """
    account = principal.require_account()
    async with context.database.connection() as connection:
        await load_chart(connection, principal, chart_id)
        await charts_db.remove_like(connection, chart_id, account.sonolus_id)
    return Response(status_code=204)


def scale_trend(values: list[int]) -> list[int]:
    """Scale running totals to 1-100.

    Args:
        values: Running totals.

    Returns:
        list[int]: Scaled values; a flat series is all 1.
    """
    low, high = min(values), max(values)
    if high == low:
        return [1 for _ in values]
    return [int(1 + 99 * (value - low) / (high - low)) for value in values]


@router.get("/{chart_id}/trends", response_model=schemas.Trends)
async def get_trends(
    chart_id: str,
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[Principal, Depends(auth())],
) -> schemas.Trends:
    """Return a chart's seven-day like and comment trends.

    Args:
        chart_id: Chart id.
        context: App context.
        principal: The caller.

    Returns:
        schemas.Trends: The trends.
    """
    async with context.database.connection() as connection:
        await load_chart(connection, principal, chart_id)
        likes = await charts_db.like_trend(connection, chart_id)
        comments = await charts_db.comment_trend(connection, chart_id)
    return schemas.Trends(likes=scale_trend(likes), comments=scale_trend(comments))
