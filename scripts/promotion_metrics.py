import argparse
import asyncio
from typing import Optional

import asyncpg
import yaml


async def run(promotion_id: Optional[int]) -> None:
    with open("config.yml", "r") as f:
        config = yaml.safe_load(f)

    psql = config["psql"]
    conn = await asyncpg.connect(
        host=psql["host"],
        user=psql["user"],
        database=psql["database"],
        password=psql["password"],
        port=psql["port"],
        ssl="disable",
    )
    try:
        select = """
            SELECT
                p.id,
                p.chart_id,
                ROUND(c.rating)::int AS chart_rating,
                p.target_type,
                p.target_amount,
                p.view_count,
                p.click_count,
                CASE
                    WHEN p.cancelled THEN 'Cancelled'
                    WHEN (p.target_type = 'VIEW' AND p.view_count >= p.target_amount)
                        OR (p.target_type = 'CLICK' AND p.click_count >= p.target_amount)
                    THEN 'Complete'
                    ELSE 'Ongoing'
                END AS status
            FROM promotions p
            LEFT JOIN charts c ON p.chart_id = c.id
        """
        if promotion_id is not None:
            rows = await conn.fetch(select + " WHERE p.id = $1;", promotion_id)
        else:
            rows = await conn.fetch(select + " ORDER BY p.id DESC;")

        if not rows:
            print("No promotions found.")
            return

        for row in rows:
            target = row["target_amount"] or 0
            views = row["view_count"]
            clicks = row["click_count"]
            progress = views if row["target_type"] == "VIEW" else clicks
            # counts can run past the target after completion, so cap the display
            percent = min(100, round(progress / target * 100)) if target else 0
            level = row["chart_rating"] if row["chart_rating"] is not None else "?"
            unit = "views" if row["target_type"] == "VIEW" else "clicks"
            print(
                f"Promotion {row['id']} | chart {row['chart_id']} (level {level}) | "
                f"{row['status']} | target: {target} {unit}"
            )
            print(f"  Completed Percentage: {percent}%")
            print(f"  Views: {views}")
            print(f"  Clicks: {clicks}")
    finally:
        await conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Show promotion metrics")
    p.add_argument(
        "--id",
        type=int,
        default=None,
        help="Only show this promotion (default: all promotions)",
    )
    asyncio.run(run(p.parse_args().id))


if __name__ == "__main__":
    main()
