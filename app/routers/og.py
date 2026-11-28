import asyncio
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.context import AppContext, get_context
from app.database import accounts as accounts_db
from app.database import charts as charts_db
from app.database import permissions as permissions_db
from app.database.charts import ChartFilters, ChartRow
from app.errors import not_found
from app.models import present
from app.services import og_cache, og_images
from app.services.chart_access import check_chart_id

router = APIRouter(prefix="/v1/og", tags=["og"])

STAFF_PICK_JACKETS = 3
TOP_CHART_JACKETS = 3
MAX_TEXT_LENGTH = 200


def png_response(context: AppContext, png: bytes) -> Response:
    """Wrap a PNG in a cacheable response.

    Args:
        context: App context.
        png: Image.

    Returns:
        Response: The response.
    """
    seconds = context.constants.og_images.browser_cache_seconds
    return Response(
        content=png,
        media_type=og_cache.CONTENT_TYPE,
        headers={"Cache-Control": f"public, max-age={seconds}"},
    )


async def fetch_files(context: AppContext, keys: list[str | None]) -> list[bytes | None]:
    """Download stored images, giving None for missing or unreadable ones.

    Args:
        context: App context.
        keys: Storage keys, None for no image.

    Returns:
        list[bytes | None]: Contents in the same order.
    """

    async def fetch(key: str | None) -> bytes | None:
        """Download one image.

        Args:
            key: Storage key, None for no image.

        Returns:
            bytes | None: Contents, or None.
        """
        if key is None:
            return None
        try:
            stored = await context.storage.get(key)
        except Exception:
            return None
        return stored[0] if stored else None

    return list(await asyncio.gather(*(fetch(key) for key in keys)))


def jacket_key(chart: ChartRow) -> str:
    """Storage key of a chart's jacket.

    Args:
        chart: Chart row.

    Returns:
        str: The key.
    """
    return present.chart_file_key(chart, chart.jacket_file_hash)


def account_image_key(sonolus_id: str, kind: str, file_hash: str | None) -> str | None:
    """Storage key of an avatar or banner.

    Args:
        sonolus_id: Account id.
        kind: ``profile`` or ``banner``.
        file_hash: Stored image hash.

    Returns:
        str | None: The key, or None without an image.
    """
    return f"{sonolus_id}/{kind}/{file_hash}" if file_hash else None


def staff_role(permissions: frozenset[str]) -> str | None:
    """Name the staff role shown on a profile image.

    Args:
        permissions: The account's staff permissions.

    Returns:
        str | None: ``Admin``, ``Moderator`` or None.
    """
    if "staff.manage" in permissions:
        return "Admin"
    return "Moderator" if permissions else None


@router.get("/site", response_class=Response)
async def site_image(
    context: Annotated[AppContext, Depends(get_context)],
    title: Annotated[str | None, Query(max_length=MAX_TEXT_LENGTH)] = None,
    subtitle: Annotated[str | None, Query(max_length=MAX_TEXT_LENGTH)] = None,
) -> Response:
    """Return the link preview image of the home page or a listed page.

    Args:
        context: App context.
        title: Page title, None for the home page design; a title and subtitle pair missing from
            og_images.site_pages also gets the home page design.
        subtitle: Line under the title, None for the default tagline.

    Returns:
        Response: The PNG.
    """
    pages = {(page.title, page.subtitle) for page in context.constants.og_images.site_pages}
    if (title, subtitle) not in pages:
        title = subtitle = None
    async with context.database.connection() as connection:
        picks, _ = await charts_db.list_charts(
            connection, ChartFilters(staff_pick=True), 0, STAFF_PICK_JACKETS
        )
    keys = [jacket_key(chart) for chart in picks]

    async def draw() -> Callable[[], bytes]:
        """Fetch the staff pick jackets.

        Returns:
            Callable[[], bytes]: The renderer.
        """
        jackets = await fetch_files(context, list(keys))
        return lambda: og_images.render_generic_og(title, subtitle, jackets)

    png = await og_cache.get_png(
        context, "site", {"title": title, "subtitle": subtitle, "jackets": keys}, draw
    )
    return png_response(context, png)


@router.get("/charts/{chart_id}", response_class=Response)
async def chart_image(
    chart_id: str, context: Annotated[AppContext, Depends(get_context)]
) -> Response:
    """Return a chart's link preview image; public, unlisted and scheduled charts only.

    Args:
        chart_id: Chart id.
        context: App context.

    Returns:
        Response: The PNG.
    """
    check_chart_id(chart_id)
    async with context.database.connection() as connection:
        chart = await charts_db.get_chart(connection, chart_id)
        if (
            chart is None
            or chart.hidden
            or (chart.status == "PRIVATE" and chart.scheduled_publish is None)
        ):
            raise not_found("error.chart.not_found")
        owner = await accounts_db.get_account(connection, chart.author)
    background_hash = (
        chart.background_file_hash or chart.background_v3_file_hash or chart.background_v1_file_hash
    )
    keys = [
        jacket_key(chart),
        present.chart_file_key(chart, background_hash),
        account_image_key(chart.author, "profile", owner.profile_hash if owner else None),
    ]
    inputs = {
        "title": chart.title,
        "artists": chart.artists,
        "author": chart.charter or chart.charter_full,
        "description": chart.description,
        "rating": str(chart.rating),
        "likes": chart.like_count,
        "comments": chart.comment_count,
        "staff_pick": chart.staff_pick,
        "files": keys,
    }

    async def draw() -> Callable[[], bytes]:
        """Fetch the jacket, background and owner avatar.

        Returns:
            Callable[[], bytes]: The renderer.
        """
        jacket, background, avatar = await fetch_files(context, list(keys))
        return lambda: og_images.render_level_og(
            title=chart.title,
            artists=chart.artists,
            author=chart.charter or chart.charter_full,
            description=chart.description,
            rating=float(chart.rating),
            like_count=chart.like_count,
            comment_count=chart.comment_count,
            staff_pick=chart.staff_pick,
            jacket=jacket,
            background=background,
            avatar=avatar,
        )

    png = await og_cache.get_png(context, "charts", inputs, draw)
    return png_response(context, png)


@router.get("/accounts/{account_id}", response_class=Response)
async def account_image(
    account_id: str, context: Annotated[AppContext, Depends(get_context)]
) -> Response:
    """Return an account's link preview image.

    Args:
        account_id: Account id.
        context: App context.

    Returns:
        Response: The PNG.
    """
    async with context.database.connection() as connection:
        account = await accounts_db.get_account(connection, account_id)
        if account is None:
            raise not_found("error.account.not_found")
        permissions = await permissions_db.get_permissions(connection, account_id)
        stats = await accounts_db.get_stats(connection, account_id)
        top, _ = await charts_db.list_charts(
            connection,
            ChartFilters(owned_by=account_id, sort_by="likes"),
            0,
            TOP_CHART_JACKETS,
        )
    role = staff_role(permissions)
    keys = [
        account_image_key(account_id, "profile", account.profile_hash),
        account_image_key(account_id, "banner", account.banner_hash),
        *(jacket_key(chart) for chart in top),
    ]
    counts = (
        (stats.charts_published, stats.likes_received, stats.comments_received)
        if stats
        else (0, 0, 0)
    )
    inputs = {
        "username": account.sonolus_username,
        "handle": account.sonolus_handle,
        "role": role,
        "bio": account.description,
        "counts": counts,
        "files": keys,
    }

    async def draw() -> Callable[[], bytes]:
        """Fetch the avatar, banner and top chart jackets.

        Returns:
            Callable[[], bytes]: The renderer.
        """
        avatar, banner, *jackets = await fetch_files(context, list(keys))
        return lambda: og_images.render_user_og(
            username=account.sonolus_username,
            handle=account.sonolus_handle,
            role=role,
            bio=account.description,
            charts=counts[0],
            likes=counts[1],
            comments=counts[2],
            avatar=avatar,
            banner=banner,
            jackets=jackets,
        )

    png = await og_cache.get_png(context, "accounts", inputs, draw)
    return png_response(context, png)
