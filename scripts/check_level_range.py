import argparse
import asyncio

from app.services.promotion_targeting import level_center_and_sigma
from scripts._common import (
    add_config_argument,
    add_constants_argument,
    connect,
    find_account,
    read_config,
    read_constants,
)


async def run(args: argparse.Namespace) -> None:
    """Print the rating range promotion targeting assumes for an account.

    Args:
        args: Parsed command line.
    """
    config = read_config(args.config)
    promotion_constants = read_constants(args.constants).promotions
    conn = await connect(config.database)
    try:
        row = await find_account(conn, args.id, args.handle)
        if not row:
            print("Account not found.")
            return

        levels = list(row.recent_opened_levels or [])
        print(f"Account: {row.sonolus_username}#{row.sonolus_handle} ({row.sonolus_id})")
        print(f"Tracked opens: {len(levels)}")

        info = level_center_and_sigma(levels, promotion_constants)
        if info is None:
            minimum = promotion_constants.min_opens_for_range
            if len(levels) < minimum:
                print(f"Level range: not decided (needs at least {minimum} opens)")
            else:
                print("Level range: undetermined (opens too spread out)")
            return

        center, sigma = info
        print("Level range: decided")
        print(f"  Center (median): {center:g}")
        print(f"  Spread (sigma): {sigma:g}")
        print(f"  Approx range: {round(center - sigma)} - {round(center + sigma)}")
        print(f"  Observed min/max: {min(levels)} / {max(levels)}")
    finally:
        await conn.close()


def main() -> None:
    """Parse the command line and print the account's level range."""
    p = argparse.ArgumentParser(description="Check an account's tracked level range")
    add_config_argument(p)
    add_constants_argument(p)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--handle", type=int, help="Sonolus handle")
    group.add_argument("--id", help="Sonolus id")
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
