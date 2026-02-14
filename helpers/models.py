import json
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Literal, Optional, TypeAlias
from datetime import datetime, date
from typing import Any, Union
from decimal import Decimal, ROUND_HALF_UP


# trend models
class ChartLikeTrend(BaseModel):
    day: date
    total_likes: int


class ChartCommentTrend(BaseModel):
    day: date
    total_comments: int


# models


class ServiceUserProfile(BaseModel):
    id: str  # ServiceUserId... is just a string.
    handle: str
    name: str
    avatarType: str
    avatarForegroundType: str
    avatarForegroundColor: str
    avatarBackgroundType: str
    avatarBackgroundColor: str
    bannerType: str
    aboutMe: str
    favorites: List[str]


class ServerAuthenticateRequest(BaseModel):
    type: str
    address: str
    time: int
    userProfile: ServiceUserProfile


class CommentRequest(BaseModel):
    content: str


class Like(BaseModel):
    type: Literal["like", "unlike"]


class ServiceUserProfileWithType(ServiceUserProfile):
    type: Literal["game"]


class ExternalServiceUserProfileWithType(ServiceUserProfile):
    type: Literal["external"]
    id_key: str


class ChartVisibilityData(BaseModel):
    status: Literal["PUBLIC", "PRIVATE", "UNLISTED"]


class ChartScheduleData(BaseModel):
    publish_time: Optional[int]  # epoch


class ChartUploadData(BaseModel):
    rating: int
    title: str
    author: str
    artists: str

    tags: Optional[List[str]] = []
    description: Optional[str] = None
    # optional, can be False
    includes_background: bool = False
    includes_preview: bool = False


class ChartStPickData(BaseModel):
    value: bool


class ChartEditData(BaseModel):
    author: Optional[str] = None
    rating: Optional[int] = None
    title: Optional[str] = None
    artists: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = []

    # files
    includes_background: Optional[bool] = False
    includes_preview: Optional[bool] = False
    delete_background: Optional[bool] = False
    delete_preview: Optional[bool] = False
    includes_audio: Optional[bool] = False
    includes_jacket: Optional[bool] = False
    includes_chart: Optional[bool] = False


class SessionKeyData(BaseModel):
    id: str
    user_id: str
    type: Literal["game", "external"]


class OAuth(BaseModel):
    access_token: str
    refresh_token: str
    expires_at: int


class SessionData(BaseModel):
    session_key: str
    expires: int


class PublicAccount(BaseModel):
    sonolus_id: str
    sonolus_handle: int
    sonolus_username: str
    profile_hash: Optional[str]
    banner_hash: Optional[str]
    description: Optional[str]
    mod: bool = False
    admin: bool = False
    banned: bool = False


class Account(PublicAccount):
    discord_id: Optional[int] = None
    patreon_id: Optional[str] = None
    chart_upload_cooldown: Optional[datetime] = None
    sonolus_sessions: Optional[
        dict[Literal["game", "external"], dict[int, SessionData]]
    ] = None
    oauth_details: Optional[dict[str, OAuth]] = None
    subscription_details: Optional[Any] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("sonolus_sessions", "oauth_details", mode="before")
    @classmethod
    def parse_json(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                raise ValueError("Invalid JSON string for dict field")
        return v


class Chart(BaseModel):
    # THIS IS FOR INCOMING API REQUESTS ONLY!
    id: str
    author: str
    rating: int
    chart_author: str
    title: str
    artists: Optional[str] = None
    jacket_file_hash: str
    music_file_hash: str
    chart_file_hash: str
    background_v1_file_hash: str
    background_v3_file_hash: str
    tags: Optional[List[str]] = Field(default_factory=list)
    description: Optional[str] = None
    preview_file_hash: Optional[str] = None
    background_file_hash: Optional[str] = None


class Count(BaseModel):
    total_count: int


class ChartDBResponse(BaseModel):
    id: str
    rating: Union[int, Decimal]
    author: str  # author sonolus id
    author_handle: int  # author sonolus handle
    title: str
    staff_pick: bool
    artists: Optional[str] = None
    jacket_file_hash: str
    music_file_hash: str
    chart_file_hash: str
    background_v1_file_hash: str
    background_v3_file_hash: str
    tags: Optional[List[str]] = Field(default_factory=list)
    description: Optional[str] = None
    preview_file_hash: Optional[str] = None
    background_file_hash: Optional[str] = None
    status: Literal["UNLISTED", "PRIVATE", "PUBLIC"]
    like_count: int
    comment_count: int
    created_at: datetime
    published_at: Optional[datetime] = None
    updated_at: datetime
    author_full: Optional[str] = None
    chart_design: str
    is_first_publish: Optional[bool] = None  # only returned on update_status
    scheduled_publish: Optional[datetime]

    model_config = {"json_encoders": {Decimal: float}}

    @model_validator(mode="before")
    def coerce_rating(cls, values):
        rating = values.get("rating")

        if rating is None:
            return values

        if isinstance(rating, float):
            rating = Decimal(str(rating))

        if isinstance(rating, Decimal):
            rating = rating.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            # Convert .0 to int
            if rating == rating.to_integral():
                rating = int(rating)

        elif isinstance(rating, int):
            rating = int(rating)

        values["rating"] = rating
        return values


class ChartDBResponseLiked(ChartDBResponse):
    liked: bool


class ChartByID(ChartDBResponse):
    log_like_score: float


class ChartByIDLiked(ChartByID):
    liked: bool


class CommentID(BaseModel):
    id: int


class Comment(BaseModel):
    id: int
    commenter: str
    username: Optional[str] = None
    content: str
    created_at: datetime
    deleted_at: Optional[datetime] = None
    chart_id: str
    owner: Optional[bool] = None


class ExternalLogin(BaseModel):
    session_key: Optional[str] = None
    expires_at: datetime
    id_key: str


class ExternalLoginKey(BaseModel):
    id_key: str


class ExternalLoginKeyData(BaseModel):
    id: str


class DBID(BaseModel):
    id: str


class ChartConstantData(BaseModel):
    constant: Decimal


class Notification(BaseModel):
    id: int
    user_id: str
    title: str
    content: Optional[str] = None
    is_read: bool = False
    created_at: datetime = None


class NotificationList(BaseModel):
    id: int
    title: str
    is_read: bool
    created_at: datetime


class NotificationRequest(BaseModel):
    user_id: Optional[str] = None
    chart_id: Optional[str] = None
    title: str
    content: Optional[str] = None


class ReadUpdate(BaseModel):
    is_read: bool


class ReplayUploadData(BaseModel):
    engine: str
    grade: Literal["allPerfect", "fullCombo", "pass", "fail"]
    nperfect: int
    ngreat: int
    ngood: int
    nmiss: int
    arcade_score: int
    accuracy_score: int
    speed: float


class LeaderboardRecord(ReplayUploadData):
    submitter: str
    display_name: str
    replay_data_hash: str
    replay_config_hash: str
    chart_id: str
    public_chart: bool


class LeaderboardRecordDBResponse(LeaderboardRecord):
    display_name: str
    id: int
    created_at: datetime
    chart_prefix: str
    owner: bool | None = None


class Prefix(BaseModel):
    prefix: str


class _ReplayData_playArea(BaseModel):
    width: int | float
    height: int | float


class GameplayResult(BaseModel):
    grade: Literal["allPerfect", "fullCombo", "pass", "fail"]
    arcadeScore: int | float
    accuracyScore: int | float
    combo: int | float
    perfect: int | float
    great: int | float
    good: int | float
    miss: int | float
    totalCount: int | float


class _ReplayData_entities_data(BaseModel):
    name: str
    value: int | float


class _ReplayData_entities(BaseModel):
    data: list[_ReplayData_entities_data]


class _ReplayData_touches(BaseModel):
    l: list[int | float]
    t: list[int | float]
    x: list[int | float]
    y: list[int | float]


class _ReplayData_streams(BaseModel):
    id: int | float
    keys: list[int | float]
    values: list[int | float]


class ReplayData(BaseModel):
    startTime: int | float
    saveTime: int | float
    duration: int | float
    inputOffset: int | float
    playArea: _ReplayData_playArea
    result: GameplayResult
    entities: list[_ReplayData_entities]
    touches: _ReplayData_touches
    streams: list[_ReplayData_streams] | None


class UserProfile(BaseModel):
    account: PublicAccount
    charts: list[ChartDBResponse]
    asset_base_url: str


class UserStats(BaseModel):
    sonolus_id: str
    sonolus_handle: int
    # interaction stats
    liked_charts_count: int
    comments_count: int
    # chart stats
    charts_published: int
    likes_received: int
    comments_received: int


leaderboard_type: TypeAlias = Literal[
    "arcade_score_speed",
    "accuracy_score",
    "arcade_score_no_speed",
    "rank_match",
    "least_combo_breaks",
    "least_misses",
    "perfect",
]


class UpdateDescriptionRequest(BaseModel):
    description: Optional[str]
