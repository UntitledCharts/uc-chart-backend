import argparse
import asyncio
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from app.database import charts as charts_db
from app.database import promotions as promotions_db
from app.database.promotions import PromotionRow
from scripts._common import add_config_argument, connect, read_config


def _format_duration(seconds: float) -> str:
    """Format a duration like ``1d 2h 3m 4s``.

    Args:
        seconds: Duration in seconds.

    Returns:
        str: The formatted duration.
    """
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _status(row: PromotionRow) -> str:
    """Classify a promotion.

    Args:
        row: The promotion.

    Returns:
        str: ``Cancelled``, ``Complete`` or ``Ongoing``.
    """
    if row.cancelled:
        return "Cancelled"
    if (row.target_type == "VIEW" and row.view_count >= row.target_amount) or (
        row.target_type == "CLICK" and row.click_count >= row.target_amount
    ):
        return "Complete"
    return "Ongoing"


async def run(promotion_id: int | None, config_path: str) -> None:
    """Print promotion metrics.

    Args:
        promotion_id: Only this promotion, or None for all.
        config_path: Location of config.yml.
    """
    config = read_config(config_path)
    conn = await connect(config.database)
    try:
        if promotion_id is not None:
            row = await promotions_db.get_promotion(conn, promotion_id)
            rows = [row] if row is not None else []
        else:
            rows = sorted(
                await promotions_db.list_promotions(conn),
                key=lambda item: item.id,
                reverse=True,
            )

        if not rows:
            print("No promotions found.")
            return

        charts = await charts_db.get_charts(conn, [row.chart_id for row in rows])
        for row in rows:
            target = row.target_amount or 0
            views = row.view_count
            clicks = row.click_count
            progress = views if row.target_type == "VIEW" else clicks
            percent = min(100, round(progress / target * 100)) if target else 0
            chart = charts.get(row.chart_id)
            level = (
                int(chart.rating.quantize(Decimal(1), rounding=ROUND_HALF_UP))
                if chart is not None
                else "?"
            )
            unit = "views" if row.target_type == "VIEW" else "clicks"
            end = row.ended_at or datetime.now(timezone.utc)
            created = row.created_at or end
            time_taken = _format_duration((end - created).total_seconds())
            time_label = "Time Taken" if row.ended_at else "Time Elapsed"
            print(
                f"Promotion {row.id} | chart {row.chart_id} (level {level}) | "
                f"{_status(row)} | target: {target} {unit}"
            )
            logged_in_clicks = row.logged_in_click_count
            print(f"  Completed Percentage: {percent}%")
            print(f"  Views: {views}")
            print(f"  Clicks: {clicks}")
            print(f"  Logged-in Clicks: {logged_in_clicks}")
            print(f"  Guest Clicks: {clicks - logged_in_clicks}")
            print(f"  {time_label}: {time_taken}")
    finally:
        await conn.close()


def main() -> None:
    """Parse the command line and print the metrics."""
    p = argparse.ArgumentParser(description="Show promotion metrics")
    add_config_argument(p)
    p.add_argument(
        "--id",
        type=int,
        default=None,
        help="Only show this promotion (default: all promotions)",
    )
    args = p.parse_args()
    asyncio.run(run(args.id, args.config))


if __name__ == "__main__":
    main()
