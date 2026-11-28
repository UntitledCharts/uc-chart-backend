from datetime import datetime
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

ItemT = TypeVar("ItemT")


def decimal_number(value: Decimal) -> int | float:
    """Serialize a decimal as a JSON number.

    Args:
        value: The decimal.

    Returns:
        int | float: An int when integral, otherwise a float.
    """
    return int(value) if value == value.to_integral_value() else float(value)


Number = Annotated[
    Decimal, PlainSerializer(decimal_number, return_type=int | float, when_used="json")
]


class Schema(BaseModel):
    """Base for request and response bodies.

    Attributes:
        model_config: Rejects unknown request fields.
    """

    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def _no_nul(cls, value: object) -> object:
        """Reject text containing NUL, which PostgreSQL can't store.

        Args:
            value: Field value.

        Returns:
            object: The value, unchanged.
        """
        texts = value if isinstance(value, list) else [value]
        if any(isinstance(text, str) and "\x00" in text for text in texts):
            raise ValueError("text can't contain NUL")
        return value


class FileRef(Schema):
    """A stored file.

    Attributes:
        hash: File hash.
        url: Public URL.
    """

    hash: str
    url: str


class ImageRef(FileRef):
    """A stored profile image.

    Attributes:
        webp_url: Public URL of the WEBP variant.
    """

    webp_url: str


class Page(Schema, Generic[ItemT]):
    """One page of a listing.

    Attributes:
        items: Items on this page.
        page: Zero-based page number.
        page_count: Total number of pages.
    """

    items: list[ItemT]
    page: int
    page_count: int


class AccountSummary(Schema):
    """Short account reference.

    Attributes:
        id: Sonolus account id.
        handle: Sonolus handle.
        username: Sonolus display name.
    """

    id: str
    handle: int
    username: str


class AccountBadge(Schema):
    """A badge an account holds.

    Attributes:
        badge: Badge name.
        level: Badge level.
        image: Image URL of the badge at this level.
        description_key: Localization key of the badge description.
    """

    badge: str
    level: int
    image: str
    description_key: str


class BadgeLevel(Schema):
    """One level of a badge.

    Attributes:
        level: Level number.
        image: Image URL of the badge at this level.
    """

    level: int
    image: str


class BadgeInfo(Schema):
    """A badge accounts can be given.

    Attributes:
        badge: Badge name.
        max_level: Highest level it can be granted at.
        description_key: Localization key of the badge description.
        automatic: Whether staff permissions give it instead of a grant.
        levels: Every level with its image.
    """

    badge: str
    max_level: int
    description_key: str
    automatic: bool
    levels: list[BadgeLevel]


class TaggedAccount(AccountSummary):
    """Account reference with the flags clients show as tags.

    Attributes:
        avatar: Avatar image, if set.
        banned: Whether the account is banned.
        permissions: Staff permissions the account holds.
        badges: Badges the account holds.
    """

    avatar: ImageRef | None = None
    banned: bool
    permissions: list[str]
    badges: list[AccountBadge]


class PublicAccount(AccountSummary):
    """Public profile data.

    Attributes:
        description: Profile description.
        avatar: Avatar image.
        banner: Banner image.
        banned: Whether the account is banned.
        permissions: Staff permissions the account holds.
        badges: Badges the account holds.
        created_at: Account creation time.
        pending_deletion: Whether the account is scheduled for deletion.
    """

    description: str | None = None
    avatar: ImageRef | None = None
    banner: ImageRef | None = None
    banned: bool
    permissions: list[str]
    badges: list[AccountBadge]
    created_at: datetime | None = None
    pending_deletion: bool


class ChartFiles(Schema):
    """A chart's files.

    Attributes:
        jacket: Jacket image.
        music: Audio.
        chart: Gzipped next_sekai level data.
        preview: Preview audio.
        background: Uploaded background.
        background_v1: Generated v1 background.
        background_v3: Generated v3 background.
    """

    jacket: FileRef
    music: FileRef
    chart: FileRef
    preview: FileRef | None = None
    background: FileRef | None = None
    background_v1: FileRef
    background_v3: FileRef


class Chart(Schema):
    """A chart.

    Attributes:
        id: Chart id.
        title: Title.
        artists: Artists.
        description: Description.
        tags: Tags.
        rating: Difficulty rating.
        status: Visibility.
        staff_pick: Whether staff picked it.
        owner: Owning account.
        charter: Free-text charter name.
        like_count: Likes.
        comment_count: Comments, including deleted ones.
        liked: Whether the viewer liked it, when the viewer is logged in.
        files: Stored files.
        created_at: Upload time.
        updated_at: Last update time.
        published_at: First publish time.
        scheduled_publish: Scheduled publish time.
        deleted_at: Soft-deletion time, shown to staff only.
        owner_pending_deletion: Whether the owner's account is scheduled for deletion.
    """

    id: str
    title: str
    artists: str | None = None
    description: str | None = None
    tags: list[str]
    rating: Number
    status: Literal["PUBLIC", "UNLISTED", "PRIVATE"]
    staff_pick: bool
    owner: AccountSummary
    charter: str
    like_count: int
    comment_count: int
    liked: bool | None = None
    files: ChartFiles
    created_at: datetime | None = None
    updated_at: datetime | None = None
    published_at: datetime | None = None
    scheduled_publish: datetime | None = None
    deleted_at: datetime | None = None
    owner_pending_deletion: bool = False


class ChartViewer(Schema):
    """What the viewer may do with a chart.

    Attributes:
        is_owner: Whether the viewer owns the chart.
        chart_permissions: Collaborator permissions on this chart (every one for the owner).
        staff_permissions: Staff permissions usable on this chart.
    """

    is_owner: bool
    chart_permissions: list[str]
    staff_permissions: list[str]


class ChartDetails(Chart):
    """A chart with the viewer's permissions.

    Attributes:
        viewer: The viewer's permissions.
    """

    viewer: ChartViewer


class ScheduledChart(Schema):
    """Public data of a chart waiting to be published.

    Attributes:
        id: Chart id.
        title: Title.
        artists: Artists.
        charter: Free-text charter name.
        owner: Owning account.
        rating: Difficulty rating.
        jacket: Jacket image.
        background: Uploaded background.
        background_v3: Generated v3 background.
        scheduled_publish: Scheduled publish time.
    """

    id: str
    title: str
    artists: str | None = None
    charter: str
    owner: AccountSummary
    rating: Number
    jacket: FileRef
    background: FileRef | None = None
    background_v3: FileRef
    scheduled_publish: datetime


class Trends(Schema):
    """Seven-day activity of a chart, scaled to 1-100.

    Attributes:
        likes: Scaled running like totals, oldest first.
        comments: Scaled running comment totals, oldest first.
    """

    likes: list[int]
    comments: list[int]


class Comment(Schema):
    """A comment.

    Attributes:
        id: Comment id.
        chart_id: Chart id.
        author: Commenting account, None when deleted.
        content: Text, None when deleted unless the viewer may see deleted comments.
        deleted: Whether the comment was deleted.
        is_own: Whether the viewer wrote it.
        created_at: Posting time.
        deleted_at: Deletion time.
    """

    id: int
    chart_id: str
    author: TaggedAccount | None = None
    content: str | None = None
    deleted: bool
    is_own: bool
    created_at: datetime
    deleted_at: datetime | None = None


class CommentPage(Page[Comment]):
    """A page of comments.

    Attributes:
        can_moderate: Whether the viewer may delete any comment and sees deleted ones.
    """

    can_moderate: bool


class ReplayFiles(Schema):
    """A record's replay files.

    Attributes:
        data: Gzipped replay data.
        configuration: Gzipped replay configuration.
    """

    data: FileRef
    configuration: FileRef


class LeaderboardRecord(Schema):
    """A leaderboard record.

    Attributes:
        id: Record id.
        chart_id: Chart id.
        submitter: Submitting account.
        display_name: Name shown on the leaderboard.
        engine: Engine name.
        grade: Result grade.
        perfect: Perfect judgments.
        great: Great judgments.
        good: Good judgments.
        miss: Misses.
        arcade_score: Arcade score.
        accuracy_score: Accuracy score.
        speed: Playback speed, None for 1.0.
        replay: Replay files.
        is_own: Whether the viewer submitted it.
        created_at: Submission time.
    """

    id: int
    chart_id: str
    submitter: TaggedAccount | None = None
    display_name: str
    engine: str
    grade: str
    perfect: int
    great: int
    good: int
    miss: int
    arcade_score: int
    accuracy_score: int
    speed: Number | None = None
    replay: ReplayFiles
    is_own: bool
    created_at: datetime


class LeaderboardPage(Page[LeaderboardRecord]):
    """A page of a chart's leaderboard.

    Attributes:
        can_moderate: Whether the viewer may delete any record.
    """

    can_moderate: bool


class RecordDetails(Schema):
    """A record with its chart.

    Attributes:
        record: The record.
        chart: The chart.
        can_delete: Whether the viewer may delete the record.
    """

    record: LeaderboardRecord
    chart: Chart
    can_delete: bool


class GlobalRecord(Schema):
    """A record in a global listing.

    Attributes:
        record: The record.
        chart: The chart.
    """

    record: LeaderboardRecord
    chart: Chart


class Notification(Schema):
    """A notification.

    Attributes:
        id: Notification id.
        title: Title.
        content: Body text, left out in listings.
        is_read: Whether it was read.
        created_at: Sending time.
    """

    id: int
    title: str
    content: str | None = None
    is_read: bool
    created_at: datetime


class NotificationPage(Page[Notification]):
    """A page of notifications.

    Attributes:
        total: Notifications matching the filter, counted in the same snapshot as the items.
        unread_count: Unread notifications in total.
    """

    total: int
    unread_count: int


class Me(Schema):
    """The logged-in account.

    Attributes:
        account: Public profile.
        unread_notifications: Unread notifications.
        upload_cooldown_until: Time before which the account can't upload.
    """

    account: PublicAccount
    unread_notifications: int
    upload_cooldown_until: datetime | None = None


class Profile(Schema):
    """A public profile with its most liked charts.

    Attributes:
        account: Public profile.
        charts: Most liked public charts it owns.
        collaborations: Most liked public charts crediting it as a collaborator.
    """

    account: PublicAccount
    charts: list[Chart]
    collaborations: list[Chart]


class AccountSettings(Schema):
    """The logged-in account's settings.

    Attributes:
        collaboration_invites: Whether others may invite the account to collaborate on charts.
    """

    collaboration_invites: bool


class AccountStats(Schema):
    """Activity statistics of an account.

    Attributes:
        liked_charts: Charts the account liked.
        comments: Comments the account wrote.
        charts_published: Public charts the account owns.
        likes_received: Likes on the account's charts.
        comments_received: Comments on the account's charts.
    """

    liked_charts: int
    comments: int
    charts_published: int
    likes_received: int
    comments_received: int


class SessionInfo(Schema):
    """A login session.

    Attributes:
        id: Session id.
        type: Game or website.
        created_at: Creation time.
        expires_at: Expiry time.
        current: Whether this request uses it.
    """

    id: UUID
    type: Literal["game", "website"]
    created_at: datetime
    expires_at: datetime
    current: bool


class NewSession(Schema):
    """A freshly created session token.

    Attributes:
        session: Bearer token.
        expires_at: Expiry time.
    """

    session: str
    expires_at: datetime


class LoginRequest(Schema):
    """A website login request.

    Attributes:
        id: Id to put in the Sonolus external login link.
        poll_secret: Secret required to collect the session; never put it in the link.
        expires_at: Time the link stops working.
    """

    id: str
    poll_secret: str
    expires_at: datetime


class ConnectionInfo(Schema):
    """A linked sign-in provider.

    Attributes:
        provider: Provider name.
        provider_user_id: Account id at the provider.
        display_name: Display name at the provider.
        linked_at: Link time.
    """

    provider: Literal["discord", "google"]
    provider_user_id: str
    display_name: str | None = None
    linked_at: datetime


class AuthorizationStart(Schema):
    """Where to send the user to approve a sign-in provider.

    Attributes:
        url: Provider authorization URL including state and PKCE challenge.
    """

    url: str


class ProviderCallback(Schema):
    """What the provider sent back to the website.

    Attributes:
        code: Authorization code.
        state: State value from the start call.
    """

    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=4096)


class ScopeInfo(Schema):
    """An OAuth scope with its description.

    Attributes:
        scope: Scope name.
        description: What the scope allows.
    """

    scope: str
    description: str


class OAuthAuthorization(Schema):
    """A third-party app the account authorized.

    Attributes:
        client_id: App client id.
        name: App name.
        description: App description.
        scopes: Granted scopes.
        authorized_at: First authorization time.
        last_used_at: Latest token issue time.
    """

    client_id: str
    name: str
    description: str | None = None
    scopes: list[ScopeInfo]
    authorized_at: datetime
    last_used_at: datetime


class Collaborator(Schema):
    """A chart collaborator.

    Attributes:
        account: Collaborator account.
        permissions: Chart permissions.
        added_at: Join time.
    """

    account: AccountSummary
    permissions: list[str]
    added_at: datetime


class CollaboratorInvite(Schema):
    """A pending collaboration invite.

    Attributes:
        id: Invite id.
        chart_id: Chart id.
        chart_title: Chart title.
        account_id: Invited account.
        permissions: Offered permissions.
        invited_by: Inviting account.
        created_at: Invite time.
    """

    id: UUID
    chart_id: str
    chart_title: str
    account_id: str
    permissions: list[str]
    invited_by: str | None = None
    created_at: datetime


class CollaboratorList(Schema):
    """A chart's collaborators and pending invites.

    Attributes:
        collaborators: Current collaborators.
        invites: Pending invites.
    """

    collaborators: list[Collaborator]
    invites: list[CollaboratorInvite]


class PermissionGrant(Schema):
    """A staff permission held by an account.

    Attributes:
        permission: Permission name.
        granted_by: Granting account, None for the admin key.
        granted_at: Grant time.
    """

    permission: str
    granted_by: str | None = None
    granted_at: datetime


class StaffAction(Schema):
    """A logged staff action.

    Attributes:
        id: Log entry id.
        actor: Acting account, None for the admin key or a deleted account.
        via_admin_key: Whether the admin key performed it.
        action: Action name.
        target_type: Kind of target.
        target_id: Target id.
        previous_value: Value before.
        new_value: Value after.
        created_at: Time of the action.
    """

    id: int
    actor: AccountSummary | None = None
    via_admin_key: bool
    action: str
    target_type: str
    target_id: str
    previous_value: str | None = None
    new_value: str | None = None
    created_at: datetime


class ServedPromotion(Schema):
    """A promotion picked for the viewer.

    Attributes:
        chart: The promoted chart.
        view_code: Code to send when the viewer opens it.
    """

    chart: Chart
    view_code: str


class PromotionServe(Schema):
    """Result of asking for a promotion.

    Attributes:
        promotion: The promotion, or None when none is shown.
    """

    promotion: ServedPromotion | None = None


class SiteAssets(Schema):
    """Shared site images, served from storage.

    Attributes:
        banner: Server banner, also the default profile banner, as PNG.
        banner_webp: The same banner as WebP, smaller for the website.
    """

    banner: FileRef | None = None
    banner_webp: FileRef | None = None


class Limits(Schema):
    """Upload limits.

    Attributes:
        files: Largest accepted upload per file kind, in bytes.
        text: Longest accepted text per field, plus ``tags_count``.
        rating: Rating bounds and decimal places.
        min_music_seconds: Shortest song accepted, in seconds.
    """

    files: dict[str, int]
    text: dict[str, int]
    rating: dict[str, int]
    min_music_seconds: float
