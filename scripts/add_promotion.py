import argparse
import asyncio
from decimal import Decimal

from app.database import charts as charts_db
from app.database import promotions as promotions_db
from app.services.chart_access import CHART_ID_PATTERN
from scripts._common import add_config_argument, connect, read_config


async def run(args: argparse.Namespace, target_type: str, target_amount: int) -> None:
    """Create a promotion for a public chart.

    Args:
        args: Parsed command line.
        target_type: ``VIEW`` or ``CLICK``.
        target_amount: Views or clicks until the promotion ends.
    """
    config = read_config(args.config)
    chart_id = args.chart_id.removeprefix(config.sonolus.chart_prefix)
    if not CHART_ID_PATTERN.match(chart_id):
        raise SystemExit(f"not a chart id: {args.chart_id}")
    conn = await connect(config.database)
    try:
        chart = await charts_db.get_chart(conn, chart_id)
        if not chart:
            print(f"No chart found with id {chart_id}.")
            return
        if chart.status != "PUBLIC":
            print(f"Chart {chart_id} is {chart.status}, must be PUBLIC to promote.")
            return
        if chart.hidden:
            print(f"Chart {chart_id} or its owner is pending deletion, it can't be promoted.")
            return

        rating = int(Decimal(chart.rating).to_integral_value())
        promotion_id = await promotions_db.create_promotion(
            conn, chart_id, target_type, target_amount
        )
        unit = "views" if target_type == "VIEW" else "clicks"
        print(
            f"Created promotion {promotion_id} for chart {chart_id} "
            f"(level {rating}) with a target of {target_amount} {unit}."
        )
    finally:
        await conn.close()


def main() -> None:
    """Parse the command line and create the promotion."""
    p = argparse.ArgumentParser(description="Create a promotion for a public chart")
    add_config_argument(p)
    p.add_argument("--chart-id", required=True, help="Chart id (32 chars, UnCh- prefix optional)")
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

    asyncio.run(run(args, target_type, target_amount))


if __name__ == "__main__":
    main()
