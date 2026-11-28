from typing import Annotated

from fastapi import APIRouter, Depends

from app.badges import BADGES
from app.context import AppContext, get_context
from app.models import present, schemas

router = APIRouter(prefix="/v1/badges", tags=["badges"])


@router.get("", response_model=list[schemas.BadgeInfo])
async def list_badges(
    context: Annotated[AppContext, Depends(get_context)],
) -> list[schemas.BadgeInfo]:
    """List every badge accounts can hold.

    Args:
        context: App context.

    Returns:
        list[schemas.BadgeInfo]: The badges.
    """
    return [
        schemas.BadgeInfo(
            badge=name,
            max_level=definition.max_level,
            description_key=definition.description_key,
            automatic=definition.automatic,
            levels=[
                schemas.BadgeLevel(
                    level=level, image=present.badge_image(context.storage, name, level)
                )
                for level in sorted(definition.images)
            ],
        )
        for name, definition in BADGES.items()
    ]
