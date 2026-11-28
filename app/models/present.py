from dataclasses import dataclass, field

from app import public_assets
from app.badges import BADGES, automatic_badges, display_order, image_asset
from app.database.accounts import AccountRow
from app.database.badges import BadgeRow
from app.database.charts import ChartRow
from app.database.comments import CommentRow
from app.database.leaderboards import RecordRow
from app.database.notifications import NotificationRow
from app.models import schemas
from app.services.discord_bot import ChartSummary
from app.services.storage import Storage


@dataclass(frozen=True)
class AccountTags:
    """Staff permissions and badges of the accounts shown next to some content.

    Attributes:
        permissions: Staff permissions keyed by account id.
        badges: Badges keyed by account id.
    """

    permissions: dict[str, frozenset[str]] = field(default_factory=dict)
    badges: dict[str, list[schemas.AccountBadge]] = field(default_factory=dict)


def badge_image(storage: Storage, badge: str, level: int) -> str:
    """Build the public URL of a badge level's image.

    Args:
        storage: Storage client.
        badge: Badge name.
        level: Badge level.

    Returns:
        str: The URL, or an empty string when the image is missing.
    """
    path = image_asset(badge, level)
    ref = public_assets.file_ref(storage, path) if path else None
    return ref.url if ref else ""


def account_badges(
    rows: list[BadgeRow], permissions: frozenset[str], storage: Storage
) -> list[schemas.AccountBadge]:
    """Build the badges of one account: granted ones plus the staff badge its permissions give.

    Args:
        rows: The account's granted badge rows.
        permissions: The account's staff permissions.
        storage: Storage client.

    Returns:
        list[schemas.AccountBadge]: The badges in catalog order, skipping any this version
            doesn't define.
    """
    levels = {row.badge: row.level for row in rows if row.badge in BADGES}
    for badge in automatic_badges(permissions):
        levels.setdefault(badge, 1)
    return [
        schemas.AccountBadge(
            badge=badge,
            level=level,
            image=badge_image(storage, badge, level),
            description_key=BADGES[badge].description_key,
        )
        for badge, level in sorted(levels.items(), key=lambda item: display_order(item[0]))
    ]


def account_summary(account: AccountRow) -> schemas.AccountSummary:
    """Build a short account reference.

    Args:
        account: Account row.

    Returns:
        schemas.AccountSummary: The reference.
    """
    return schemas.AccountSummary(
        id=account.sonolus_id,
        handle=account.sonolus_handle,
        username=account.sonolus_username,
    )


def _image(
    storage: Storage, sonolus_id: str, kind: str, file_hash: str | None
) -> schemas.ImageRef | None:
    """Build an avatar or banner reference.

    Args:
        storage: Storage client.
        sonolus_id: Account id.
        kind: ``profile`` or ``banner``.
        file_hash: Stored image hash.

    Returns:
        schemas.ImageRef | None: The reference, or None without an image.
    """
    if not file_hash:
        return None
    key = f"{sonolus_id}/{kind}/{file_hash}"
    return schemas.ImageRef(
        hash=file_hash,
        url=storage.public_url(key),
        webp_url=storage.public_url(key + "_webp"),
    )


def public_account(
    account: AccountRow,
    permissions: frozenset[str],
    badges: list[schemas.AccountBadge],
    storage: Storage,
) -> schemas.PublicAccount:
    """Build a public profile.

    Args:
        account: Account row.
        permissions: The account's staff permissions.
        badges: The account's badges.
        storage: Storage client.

    Returns:
        schemas.PublicAccount: The profile.
    """
    return schemas.PublicAccount(
        id=account.sonolus_id,
        handle=account.sonolus_handle,
        username=account.sonolus_username,
        description=account.description,
        avatar=_image(storage, account.sonolus_id, "profile", account.profile_hash),
        banner=_image(storage, account.sonolus_id, "banner", account.banner_hash),
        banned=account.banned,
        permissions=sorted(permissions),
        badges=badges,
        created_at=account.created_at,
        pending_deletion=account.deleted_at is not None,
    )


def chart_file_key(chart: ChartRow, file_hash: str) -> str:
    """Storage key of a chart file.

    Args:
        chart: Chart row.
        file_hash: File hash.

    Returns:
        str: The key.
    """
    return f"{chart.author}/{chart.id}/{file_hash}"


def _file(storage: Storage, chart: ChartRow, file_hash: str | None) -> schemas.FileRef | None:
    """Build a chart file reference.

    Args:
        storage: Storage client.
        chart: Chart row.
        file_hash: File hash.

    Returns:
        schemas.FileRef | None: The reference, or None without a file.
    """
    if not file_hash:
        return None
    return schemas.FileRef(hash=file_hash, url=storage.public_url(chart_file_key(chart, file_hash)))


def chart_files(chart: ChartRow, storage: Storage) -> schemas.ChartFiles:
    """Build a chart's file references.

    Args:
        chart: Chart row.
        storage: Storage client.

    Returns:
        schemas.ChartFiles: The files.
    """
    required = {
        "jacket": chart.jacket_file_hash,
        "music": chart.music_file_hash,
        "chart": chart.chart_file_hash,
        "background_v1": chart.background_v1_file_hash,
        "background_v3": chart.background_v3_file_hash,
    }
    files = {
        name: schemas.FileRef(
            hash=file_hash, url=storage.public_url(chart_file_key(chart, file_hash))
        )
        for name, file_hash in required.items()
    }
    return schemas.ChartFiles(
        jacket=files["jacket"],
        music=files["music"],
        chart=files["chart"],
        background_v1=files["background_v1"],
        background_v3=files["background_v3"],
        preview=_file(storage, chart, chart.preview_file_hash),
        background=_file(storage, chart, chart.background_file_hash),
    )


def chart(row: ChartRow, storage: Storage, show_deleted: bool = False) -> schemas.Chart:
    """Build a chart.

    Args:
        row: Chart row.
        storage: Storage client.
        show_deleted: Whether to include the soft-deletion time.

    Returns:
        schemas.Chart: The chart.
    """
    return schemas.Chart(
        id=row.id,
        title=row.title,
        artists=row.artists,
        description=row.description,
        tags=row.tags,
        rating=row.rating,
        status=row.status,
        staff_pick=row.staff_pick,
        owner=schemas.AccountSummary(
            id=row.author, handle=row.author_handle, username=row.author_username
        ),
        charter=row.charter,
        like_count=row.like_count,
        comment_count=row.comment_count,
        liked=row.liked,
        files=chart_files(row, storage),
        created_at=row.created_at,
        updated_at=row.updated_at,
        published_at=row.published_at,
        scheduled_publish=row.scheduled_publish,
        deleted_at=row.deleted_at if show_deleted else None,
        owner_pending_deletion=(row.account_deleted_at is not None if show_deleted else False),
    )


def tagged_account(
    account: AccountRow, tags: AccountTags, storage: Storage
) -> schemas.TaggedAccount:
    """Build an account reference with its avatar, tag flags and badges.

    Args:
        account: Account row.
        tags: Staff permissions and badges keyed by account id.
        storage: Storage client.

    Returns:
        schemas.TaggedAccount: The reference.
    """
    return schemas.TaggedAccount(
        id=account.sonolus_id,
        handle=account.sonolus_handle,
        username=account.sonolus_username,
        avatar=_image(storage, account.sonolus_id, "profile", account.profile_hash),
        banned=account.banned,
        permissions=sorted(tags.permissions.get(account.sonolus_id, frozenset())),
        badges=tags.badges.get(account.sonolus_id, []),
    )


def record(
    row: RecordRow,
    storage: Storage,
    viewer_id: str | None,
    accounts: dict[str, AccountRow],
    tags: AccountTags,
) -> schemas.LeaderboardRecord:
    """Build a leaderboard record.

    Args:
        row: Record row.
        storage: Storage client.
        viewer_id: Viewer account id.
        accounts: Submitting accounts keyed by id.
        tags: Staff permissions and badges keyed by account id.

    Returns:
        schemas.LeaderboardRecord: The record.
    """
    submitter = accounts.get(row.submitter)
    return schemas.LeaderboardRecord(
        id=row.id,
        chart_id=row.chart_id,
        submitter=tagged_account(submitter, tags, storage) if submitter else None,
        display_name=row.display_name,
        engine=row.engine,
        grade=row.grade,
        perfect=row.nperfect,
        great=row.ngreat,
        good=row.ngood,
        miss=row.nmiss,
        arcade_score=row.arcade_score,
        accuracy_score=row.accuracy_score,
        speed=row.speed,
        replay=schemas.ReplayFiles(
            data=schemas.FileRef(
                hash=row.replay_data_hash,
                url=storage.public_url(row.replay_prefix + row.replay_data_hash),
            ),
            configuration=schemas.FileRef(
                hash=row.replay_config_hash,
                url=storage.public_url(row.replay_prefix + row.replay_config_hash),
            ),
        ),
        is_own=viewer_id is not None and viewer_id == row.submitter,
        created_at=row.created_at,
    )


def comment(
    row: CommentRow,
    viewer_id: str | None,
    can_moderate: bool,
    accounts: dict[str, AccountRow],
    tags: AccountTags,
    storage: Storage,
) -> schemas.Comment:
    """Build a comment, hiding deleted text from non-moderators.

    Args:
        row: Comment row.
        viewer_id: Viewer account id.
        can_moderate: Whether the viewer may see deleted comments.
        accounts: Commenting accounts keyed by id.
        tags: Staff permissions and badges keyed by account id.
        storage: Storage client.

    Returns:
        schemas.Comment: The comment.
    """
    deleted = row.deleted_at is not None
    author = accounts.get(row.commenter) if row.commenter else None
    return schemas.Comment(
        id=row.id,
        chart_id=row.chart_id or "",
        author=tagged_account(author, tags, storage) if author else None,
        content=row.content if (not deleted or can_moderate) else None,
        deleted=deleted,
        is_own=viewer_id is not None and viewer_id == row.commenter,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
    )


def notification(row: NotificationRow, include_content: bool) -> schemas.Notification:
    """Build a notification.

    Args:
        row: Notification row.
        include_content: Whether to include the body text.

    Returns:
        schemas.Notification: The notification.
    """
    return schemas.Notification(
        id=row.id,
        title=row.title,
        content=row.content if include_content else None,
        is_read=row.is_read,
        created_at=row.created_at,
    )


def page_count(total: int, page_size: int) -> int:
    """Number of pages for a total.

    Args:
        total: Total items.
        page_size: Items per page.

    Returns:
        int: Page count.
    """
    return (total + page_size - 1) // page_size if page_size > 0 else 0


def discord_chart(row: ChartRow, storage: Storage) -> ChartSummary:
    """Build the chart details Discord messages use.

    Args:
        row: Chart row.
        storage: Storage client.

    Returns:
        ChartSummary: The summary.
    """
    return ChartSummary(
        row.id,
        row.title,
        row.artists,
        row.charter_full,
        storage.public_url(chart_file_key(row, row.jacket_file_hash)),
    )
