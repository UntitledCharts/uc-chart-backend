from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Section(BaseModel):
    """Base for config sections that rejects unknown keys.

    Attributes:
        model_config: Pydantic configuration forbidding extra keys.
    """

    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Section):
    """HTTP server settings.

    Attributes:
        host: Interface uvicorn binds to.
        port: Port uvicorn listens on.
        workers: Number of uvicorn worker processes.
        debug: Enables API docs and verbose errors only.
        public_url: Public base URL of this API.
        website_url: Base URL of the website that hosts consent and callback pages.
        allowed_hosts: Host headers accepted when not in debug mode.
        cors_origins: Origins allowed by CORS.
        trusted_proxies: Number of proxies in front of the app adding X-Forwarded-For entries.
        limit_max_requests: Requests after which a worker restarts to return its memory, None to never restart.
    """

    host: str = "0.0.0.0"
    port: int = 39000
    workers: int = 8
    debug: bool = False
    public_url: str
    website_url: str
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1"])
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    trusted_proxies: int = 2
    limit_max_requests: int | None = Field(default=None, gt=0)


class SonolusConfig(_Section):
    """Sonolus server settings the backend needs.

    Attributes:
        server_url: Public URL of the Sonolus server, checked against signed login payloads.
        chart_prefix: Prefix of chart item names on the Sonolus server.
    """

    server_url: str
    chart_prefix: str = "UnCh-"


class KeysConfig(_Section):
    """Secret keys.

    Attributes:
        admin_key: Value of the X-Admin-Key header granting every staff permission.
        sonoserver_key: Value of the X-SonoServer-Key header for internal Sonolus server jobs.
        token_secret: HMAC key for login poll secrets, OAuth state and viewer hashes.
        encryption_key: Base64 AES-256-GCM key for stored provider tokens.
    """

    admin_key: str
    sonoserver_key: str
    token_secret: str
    encryption_key: str


class DatabaseConfig(_Section):
    """PostgreSQL connection settings.

    Attributes:
        host: Database host.
        port: Database port.
        user: Database user.
        password: Database password.
        database: Database name.
        pool_min_size: Minimum connections kept per worker.
        pool_max_size: Maximum connections per worker.
        ssl: asyncpg ssl mode.
    """

    host: str
    port: int = 5432
    user: str
    password: str
    database: str
    pool_min_size: int = 10
    pool_max_size: int = 20
    ssl: str = "disable"


class StorageConfig(_Section):
    """S3-compatible object storage settings.

    Attributes:
        endpoint: S3 API endpoint URL.
        bucket: Bucket holding every uploaded file.
        access_key_id: S3 access key id.
        secret_access_key: S3 secret access key.
        region: S3 region name.
        public_url: Public base URL the bucket's files are served from.
    """

    endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = "auto"
    public_url: str


class DiscordChannelsConfig(_Section):
    """Discord channel ids per event; an empty id disables that event.

    Attributes:
        staff_pick: Channel for new staff picks.
        visibility: Channel for visibility and schedule changes.
        first_publish: Channel for charts published for the first time.
        new_uploads: Channel for new uploads.
        comments: Channel for new comments.
        staff_actions: Channel for staff actions.
    """

    staff_pick: str = ""
    visibility: str = ""
    first_publish: str = ""
    new_uploads: str = ""
    comments: str = ""
    staff_actions: str = ""


class DiscordConfig(_Section):
    """Discord bot settings.

    Attributes:
        bot_token: Bot token used to post messages; empty disables the bot.
        channels: Channel ids per event.
    """

    bot_token: str = ""
    channels: DiscordChannelsConfig = Field(default_factory=DiscordChannelsConfig)


class ProviderConfig(_Section):
    """Settings of one sign-in provider.

    Attributes:
        enabled: Whether linking and signing in with this provider is allowed.
        client_id: OAuth client id at the provider.
        client_secret: OAuth client secret at the provider.
        redirect_uri: Website page the provider redirects back to.
        store_tokens: Whether the provider's tokens are kept (encrypted) after linking.
    """

    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    store_tokens: bool = False


class ConnectionsConfig(_Section):
    """Sign-in provider settings.

    Attributes:
        discord: Discord provider settings.
        google: Google provider settings.
    """

    discord: ProviderConfig = Field(default_factory=lambda: ProviderConfig(store_tokens=True))
    google: ProviderConfig = Field(default_factory=ProviderConfig)


class ChartsConfig(_Section):
    """Chart processing settings.

    Attributes:
        max_decompressed_size: Largest decompressed level data accepted, in bytes.
        conversion_slots: Chart conversions allowed at once across every worker.
        conversion_queue_timeout: Seconds a chart waits for a free conversion slot.
        conversion_timeout: Seconds a conversion child may run before it is killed.
        conversion_memory_limit: Largest memory a conversion child may use, in bytes.
        max_concurrent_audio: Audio checks and re-encodes allowed at once per worker.
    """

    max_decompressed_size: int = Field(default=268435456, gt=0)
    conversion_slots: int = Field(default=1, ge=1)
    conversion_queue_timeout: float = Field(default=60, gt=0)
    conversion_timeout: float = Field(default=120, gt=0)
    conversion_memory_limit: int = Field(default=6442450944, gt=0)
    max_concurrent_audio: int = 2


class WorkerConfig(_Section):
    """Background worker settings.

    Attributes:
        interval_seconds: Seconds between worker cycles.
        cleanup_batch_size: Rows deleted per table per cycle.
    """

    interval_seconds: int = 60
    cleanup_batch_size: int = 10000


class Config(_Section):
    """Whole config.yml.

    Attributes:
        server: HTTP server settings.
        sonolus: Sonolus server settings.
        keys: Secret keys.
        database: PostgreSQL settings.
        storage: Object storage settings.
        discord: Discord bot settings.
        connections: Sign-in provider settings.
        charts: Chart processing settings.
        worker: Background worker settings.
    """

    server: ServerConfig
    sonolus: SonolusConfig
    keys: KeysConfig
    database: DatabaseConfig
    storage: StorageConfig
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    connections: ConnectionsConfig = Field(default_factory=ConnectionsConfig)
    charts: ChartsConfig = Field(default_factory=ChartsConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)


class FileLimits(_Section):
    """Upload size limits.

    Attributes:
        max_sizes: Largest accepted upload per file kind, in bytes.
        max_replay_decompressed_size: Largest decompressed replay data, in bytes.
    """

    max_sizes: dict[str, int]
    max_replay_decompressed_size: int


class TextLimits(_Section):
    """Text length limits.

    Attributes:
        max_lengths: Longest accepted text per field.
        max_tags: Most tags a chart may have.
    """

    max_lengths: dict[str, int]
    max_tags: int


class RatingLimits(_Section):
    """Chart rating limits.

    Attributes:
        min: Lowest rating.
        max: Highest rating.
        decimal_places: Most decimal places a rating may have.
    """

    min: int
    max: int
    decimal_places: int


class ImageConstants(_Section):
    """Image processing sizes.

    Attributes:
        jacket_size: Side length jackets are resized to.
        avatar_size: Width and height avatars are resized to.
        banner_size: Width and height banners are resized to.
        max_pixels: Largest image accepted, in pixels.
    """

    jacket_size: int
    avatar_size: tuple[int, int]
    banner_size: tuple[int, int]
    max_pixels: int


class ChartConstants(_Section):
    """Chart behavior constants.

    Attributes:
        page_size: Charts per listing page.
        random_count: Charts returned by the random listing.
        upload_cooldown_seconds: Seconds between uploads of one account.
        deletion_grace_days: Days before soft-deleted charts and accounts are purged.
        recent_opened_levels_kept: Opened chart ratings remembered per account.
        profile_top_charts: Charts shown on a profile.
    """

    page_size: int
    random_count: int
    upload_cooldown_seconds: int
    deletion_grace_days: int
    recent_opened_levels_kept: int
    profile_top_charts: int


class AudioConstants(_Section):
    """VBR detection and re-encoding constants.

    Attributes:
        vbr_probe_seconds: Seconds of audio sampled for VBR detection.
        vbr_tolerance: Relative bitrate spread treated as VBR.
        standard_bitrates: CBR bitrates to choose from.
        default_bitrate: Bitrate used when the average can't be read.
        probe_timeout_seconds: ffprobe timeout.
        encode_timeout_seconds: ffmpeg timeout.
    """

    vbr_probe_seconds: int
    vbr_tolerance: float
    standard_bitrates: list[int]
    default_bitrate: int
    probe_timeout_seconds: int
    encode_timeout_seconds: int


class SessionConstants(_Section):
    """Session lifetimes.

    Attributes:
        game_seconds: Lifetime of in-game sessions.
        website_seconds: Lifetime of website sessions.
        max_per_type: Sessions kept per account and type.
    """

    game_seconds: int
    website_seconds: int
    max_per_type: int


class LoginRequestConstants(_Section):
    """Website login request lifetimes.

    Attributes:
        link_seconds: Lifetime of a login link before it is approved.
        pickup_seconds: Time the website has to collect an approved session.
    """

    link_seconds: int
    pickup_seconds: int


class SonolusConstants(_Section):
    """Sonolus signature constants.

    Attributes:
        signature_time_window_seconds: Accepted clock difference of signed payloads.
    """

    signature_time_window_seconds: int


class OAuthConstants(_Section):
    """OAuth provider lifetimes.

    Attributes:
        access_token_seconds: Access token lifetime.
        refresh_token_seconds: Refresh token lifetime.
        authorization_code_seconds: Authorization code lifetime.
    """

    access_token_seconds: int
    refresh_token_seconds: int
    authorization_code_seconds: int


class ConnectionConstants(_Section):
    """Sign-in provider constants.

    Attributes:
        state_seconds: Lifetime of a signed OAuth state.
    """

    state_seconds: int


class PageConstants(_Section):
    """Single page size setting.

    Attributes:
        page_size: Items per page.
    """

    page_size: int


class LeaderboardConstants(_Section):
    """Leaderboard paging constants.

    Attributes:
        page_size: Records per page.
        preview_size: Records in a chart's leaderboard preview.
        global_max_limit: Largest limit of the global record listings.
    """

    page_size: int
    preview_size: int
    global_max_limit: int


class PromotionConstants(_Section):
    """Promotion targeting and counting constants.

    Attributes:
        view_window_hours: Hours a viewer counts once and a view code stays valid.
        view_code_length: Characters in a view code.
        view_code_attempts: Attempts to generate a unique view code.
        min_show_chance: Lowest chance of showing a promotion to a logged-in user.
        max_show_chance: Highest chance of showing a promotion to a logged-in user.
        default_show_chance: Chance for users without ad history.
        guest_show_chance: Chance for anonymous viewers.
        high_click_rate: Click rate reaching the highest chance.
        confidence_views: Views before a user's own click rate fully decides.
        min_opens_for_range: Opened levels before a preferred rating range is used.
        max_range_sigma: Spread above which no preferred range is assumed.
        min_range_sigma: Smallest spread used for a preferred range.
        below_range_sigma_factor: Spread multiplier for promotions below the range.
        frequency_half_at: Times shown before the weight is halved.
        frequency_quarter_at: Times shown before the weight is quartered.
        frequency_stop_at: Times shown before the promotion stops being shown.
    """

    view_window_hours: int
    view_code_length: int
    view_code_attempts: int
    min_show_chance: float
    max_show_chance: float
    default_show_chance: float
    guest_show_chance: float
    high_click_rate: float
    confidence_views: int
    min_opens_for_range: int
    max_range_sigma: float
    min_range_sigma: float
    below_range_sigma_factor: float
    frequency_half_at: int
    frequency_quarter_at: int
    frequency_stop_at: int


class Constants(_Section):
    """Whole constants.yml.

    Attributes:
        files: Upload size limits.
        text: Text length limits.
        rating: Chart rating limits.
        images: Image processing sizes.
        charts: Chart behavior constants.
        audio: Audio processing constants.
        sessions: Session lifetimes.
        login_requests: Website login request lifetimes.
        sonolus: Sonolus signature constants.
        oauth: OAuth provider lifetimes.
        connections: Sign-in provider constants.
        comments: Comment paging.
        notifications: Notification paging.
        leaderboards: Leaderboard paging.
        staff_actions: Staff action paging.
        promotions: Promotion constants.
    """

    files: FileLimits
    text: TextLimits
    rating: RatingLimits
    images: ImageConstants
    charts: ChartConstants
    audio: AudioConstants
    sessions: SessionConstants
    login_requests: LoginRequestConstants
    sonolus: SonolusConstants
    oauth: OAuthConstants
    connections: ConnectionConstants
    comments: PageConstants
    notifications: PageConstants
    leaderboards: LeaderboardConstants
    staff_actions: PageConstants
    promotions: PromotionConstants


def _read_yaml(path: Path) -> dict:
    """Read a YAML mapping from disk.

    Args:
        path: File to read.

    Returns:
        dict: Parsed mapping.
    """
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def load_config(path: Path) -> Config:
    """Load and validate config.yml.

    Args:
        path: Location of config.yml.

    Returns:
        Config: Validated configuration.
    """
    return Config.model_validate(_read_yaml(path))


def load_constants(path: Path) -> Constants:
    """Load and validate constants.yml.

    Args:
        path: Location of constants.yml.

    Returns:
        Constants: Validated constants.
    """
    return Constants.model_validate(_read_yaml(path))
