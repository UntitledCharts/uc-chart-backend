import argparse
import asyncio
from decimal import Decimal

import asyncpg
import yaml


async def run(chart_id: str, target_type: str, target_amount: int) -> None:
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
        chart = await conn.fetchrow(
            "SELECT status, rating FROM charts WHERE id = $1;", chart_id
        )
        if not chart:
            print(f"No chart found with id {chart_id}.")
            return
        if chart["status"] != "PUBLIC":
            print(f"Chart {chart_id} is {chart['status']}, must be PUBLIC to promote.")
            return

        rating = int(Decimal(chart["rating"]).to_integral_value())
        promotion_id = await conn.fetchval(
            """
            INSERT INTO promotions (chart_id, target_type, target_amount)
            VALUES ($1, $2, $3)
            RETURNING id;
            """,
            chart_id,
            target_type,
            target_amount,
        )
        unit = "views" if target_type == "VIEW" else "clicks"
        print(
            f"Created promotion {promotion_id} for chart {chart_id} "
            f"(level {rating}) with a target of {target_amount} {unit}."
        )
    finally:
        await conn.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Create a promotion for a public chart")
    p.add_argument("--chart-id", required=True, help="Chart id (32 chars, no UnCh-)")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--views", type=int, help="Target number of views")
    group.add_argument("--clicks", type=int, help="Target number of clicks")
    args = p.parse_args()

    if args.views is not None:
        target_type, target_amount = "VIEW", args.views
    else:
        target_type, target_amount = "CLICK", args.clicks
    if target_amount <= 0:
        p.error("target must be a positive integer")

    asyncio.run(run(args.chart_id, target_type, target_amount))


if __name__ == "__main__":
    main()
