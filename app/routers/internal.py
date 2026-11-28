import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.auth.principal import Principal, auth
from app.context import AppContext, get_context
from app.database import charts as charts_db
from app.database import leaderboards as leaderboards_db
from app.errors import bad_request
from app.services import chart_files
from app.services.chart_access import load_chart
from app.services.uploads import read_limited

router = APIRouter(prefix="/v1/internal", tags=["internal"])

MAX_SCORE = 100000000
MAX_JUDGMENTS = 1000000


class ReplayResult(BaseModel):
    """Result section of a Sonolus replay.

    Attributes:
        model_config: Ignores fields the leaderboard doesn't use.
        grade: Result grade.
        arcadeScore: Arcade score.
        accuracyScore: Accuracy score.
        perfect: Perfect judgments.
        great: Great judgments.
        good: Good judgments.
        miss: Misses.
    """

    model_config = ConfigDict(extra="ignore")

    grade: str
    arcadeScore: int = Field(ge=0, le=MAX_SCORE)
    accuracyScore: int = Field(ge=0, le=MAX_SCORE)
    perfect: int = Field(ge=0, le=MAX_JUDGMENTS)
    great: int = Field(ge=0, le=MAX_JUDGMENTS)
    good: int = Field(ge=0, le=MAX_JUDGMENTS)
    miss: int = Field(ge=0, le=MAX_JUDGMENTS)


class ReplayData(BaseModel):
    """Sonolus replay data.

    Attributes:
        model_config: Ignores fields the leaderboard doesn't use.
        result: The result section.
    """

    model_config = ConfigDict(extra="ignore")

    result: ReplayResult


class ReplaySaved(BaseModel):
    """Outcome of a replay submission.

    Attributes:
        status: ``ok`` when the record was saved, ``unchanged`` when it didn't beat the old one.
    """

    status: Literal["ok", "unchanged"]


def parse_replay(data: bytes, limit: int) -> ReplayResult:
    """Decompress and validate replay data.

    Args:
        data: Gzipped replay data.
        limit: Largest decompressed size accepted.

    Returns:
        ReplayResult: The replay result.
    """
    raw = chart_files.inflate_limited(data, limit)
    try:
        return ReplayData.model_validate(json.loads(raw)).result
    except (ValueError, ValidationError, RecursionError):
        raise bad_request("error.replay.invalid") from None


@router.post("/replays", response_model=ReplaySaved)
async def submit_replay(
    context: Annotated[AppContext, Depends(get_context)],
    principal: Annotated[
        Principal,
        Depends(auth(account=True, oauth=False, banned=False, sonoserver=True)),
    ],
    chart_id: Annotated[str, Form()],
    display_name: Annotated[str, Form()],
    engine_name: Annotated[str, Form()],
    replay_data: Annotated[UploadFile, File()],
    replay_config: Annotated[UploadFile, File()],
    speed: Annotated[float | None, Form(allow_inf_nan=False)] = None,
) -> ReplaySaved:
    """Save a player's replay as their record if it beats their previous one.

    Args:
        context: App context.
        principal: The player, authenticated by their own session.
        chart_id: Chart id.
        display_name: Name shown on the leaderboard.
        engine_name: Engine the replay was played on.
        replay_data: Gzipped replay data.
        replay_config: Gzipped replay configuration.
        speed: Playback speed.

    Returns:
        ReplaySaved: Whether the record changed.
    """
    account = principal.require_account()
    sizes = context.constants.files.max_sizes
    data = await read_limited(replay_data, sizes["replay_data"], "replay_data")
    config = await read_limited(replay_config, sizes["replay_config"], "replay_config")
    result = await context.run_blocking(
        parse_replay, data, context.constants.files.max_replay_decompressed_size
    )
    score = leaderboards_db.effective_score(result.arcadeScore, speed)
    async with context.database.connection() as connection:
        access = await load_chart(connection, principal, chart_id)
        current = await leaderboards_db.get_user_record(connection, chart_id, account.sonolus_id)
    if (
        current is not None
        and leaderboards_db.effective_score(
            current.arcade_score,
            float(current.speed) if current.speed is not None else None,
        )
        >= score
    ):
        return ReplaySaved(status="unchanged")
    chart = access.chart
    prefix = f"{chart.author}/{chart.id}/replays/{account.sonolus_id}/"
    data_hash = chart_files.sha1(data)
    config_hash = chart_files.sha1(config)
    new_keys = {prefix + data_hash, prefix + config_hash}
    try:
        await context.storage.put_many(
            [
                (prefix + data_hash, data, "application/gzip"),
                (prefix + config_hash, config, "application/gzip"),
            ]
        )
        async with context.database.transaction() as connection:
            await leaderboards_db.lock_submission(connection, chart_id, account.sonolus_id)
            gameplay = await charts_db.lock_gameplay(connection, chart_id)
            locked = await leaderboards_db.get_user_record(
                connection, chart_id, account.sonolus_id, lock=True
            )
            if (
                gameplay is None
                or gameplay.chart_file_hash != chart.chart_file_hash
                or gameplay.music_file_hash != chart.music_file_hash
                or (
                    locked is not None
                    and leaderboards_db.effective_score(
                        locked.arcade_score,
                        float(locked.speed) if locked.speed is not None else None,
                    )
                    >= score
                )
            ):
                kept = (
                    set()
                    if locked is None
                    else {
                        locked.replay_prefix + locked.replay_data_hash,
                        locked.replay_prefix + locked.replay_config_hash,
                    }
                )
                await context.storage.discard_keys(new_keys - kept)
                return ReplaySaved(status="unchanged")
            await leaderboards_db.save_record(
                connection,
                chart_id,
                account.sonolus_id,
                display_name,
                engine_name,
                result.grade,
                (result.perfect, result.great, result.good, result.miss),
                result.arcadeScore,
                result.accuracyScore,
                speed,
                data_hash,
                config_hash,
                gameplay.status == "PUBLIC",
            )
    except Exception:
        held = (
            set()
            if current is None
            else {
                current.replay_prefix + current.replay_data_hash,
                current.replay_prefix + current.replay_config_hash,
            }
        )
        await context.storage.discard_keys(new_keys - held)
        raise
    if locked is not None:
        old_keys = {
            locked.replay_prefix + locked.replay_data_hash,
            locked.replay_prefix + locked.replay_config_hash,
        }
        await context.storage.discard_keys(old_keys - new_keys)
    return ReplaySaved(status="ok")
