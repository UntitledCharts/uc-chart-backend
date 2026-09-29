import argparse
import asyncio
import os
import sys
from typing import Optional

import asyncpg
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers.promotions import _level_center_and_sigma, MIN_OPENS_FOR_RANGE


async def run(handle: Optional[int], sonolus_id: Optional[str]) -> None:
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
        columns = "sonolus_id, sonolus_handle, sonolus_username, recent_opened_levels"
        if sonolus_id is not None:
            row = await conn.fetchrow(
                f"SELECT {columns} FROM accounts WHERE sonolus_id = $1;", sonolus_id
            )
        else:
            row = await conn.fetchrow(
                f"SELECT {columns} FROM accounts WHERE sonolus_handle = $1;", handle
            )

        if not row:
            print("Account not found.")
            return

        levels = list(row["recent_opened_levels"] or [])
        print(
            f"Account: {row['sonolus_username']}#{row['sonolus_handle']} "
            f"({row['sonolus_id']})"
        )
        print(f"Tracked opens: {len(levels)}")

        info = _level_center_and_sigma(levels)
        if info is None:
            if len(levels) < MIN_OPENS_FOR_RANGE:
                print(
                    f"Level range: not decided (needs at least {MIN_OPENS_FOR_RANGE} opens)"
                )
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
    p = argparse.ArgumentParser(description="Check an account's tracked level range")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--handle", type=int, help="Sonolus handle")
    group.add_argument("--id", help="Sonolus id")
    args = p.parse_args()
    asyncio.run(run(args.handle, args.id))


if __name__ == "__main__":
    main()
