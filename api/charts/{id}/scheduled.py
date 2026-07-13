from fastapi import APIRouter, Request, HTTPException, status

from database import charts
from helpers.session import get_session, Session
from core import ChartFastAPI

router = APIRouter()


@router.get("/")
async def main(
    request: Request,
    id: str,
    # countdown data is public, an oauth token reads it like anyone else
    session: Session = get_session(scopes=[]),
):
    app: ChartFastAPI = request.app

    if len(id) != 32 or not id.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid chart ID."
        )

    query = charts.get_chart_by_id(id, sonolus_id=session.sonolus_id)

    async with app.db_acquire() as conn:
        result = await conn.fetchrow(query)

    if not result or not result.scheduled_publish:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Chart not found."
        )

    return {
        "data": {
            "id": result.id,
            "title": result.title,
            "artists": result.artists,
            "author": result.author,
            "author_full": result.author_full,
            "author_handle": result.author_handle,
            "rating": result.rating,
            "jacket_file_hash": result.jacket_file_hash,
            "background_file_hash": result.background_file_hash,
            "background_v3_file_hash": result.background_v3_file_hash,
            "scheduled_publish": (
                result.scheduled_publish.isoformat()
                if result.scheduled_publish
                else None
            ),
        },
        "asset_base_url": app.s3_asset_base_url,
    }
