import argparse
import asyncio

from typing import get_args

from app.badges import BADGES, GrantableBadge
from app.database import badges as badges_db
from scripts._common import (
    account_label,
    add_account_arguments,
    add_config_argument,
    add_constants_argument,
    describe_target,
    find_account,
    open_context,
    read_config,
    read_constants,
    record_admin_action,
)


async def run(args: argparse.Namespace) -> None:
    """List, grant or remove an account's badges as the admin key.

    Args:
        args: Parsed command line.
    """
    config = read_config(args.config)
    constants = read_constants(args.constants)
    async with open_context(config, constants) as context:
        async with context.database.transaction() as connection:
            account = await find_account(connection, args.account_id, args.handle)
            if account is None:
                raise SystemExit(
                    f"No account found for {describe_target(args.account_id, args.handle)}."
                )
            sonolus_id = account.sonolus_id
            if args.grant:
                previous = await badges_db.set_badge(
                    connection, sonolus_id, args.grant, args.level, None
                )
                if previous != args.level:
                    await record_admin_action(
                        context,
                        connection,
                        "badge_grant",
                        "account",
                        sonolus_id,
                        f"{args.grant}:{previous}" if previous is not None else None,
                        f"{args.grant}:{args.level}",
                    )
                print(f"Gave {args.grant} (level {args.level}) to {account_label(account)}.")
            elif args.remove:
                previous = await badges_db.remove_badge(connection, sonolus_id, args.remove)
                if previous is None:
                    print(f"{account_label(account)} doesn't hold {args.remove}.")
                else:
                    await record_admin_action(
                        context,
                        connection,
                        "badge_revoke",
                        "account",
                        sonolus_id,
                        f"{args.remove}:{previous}",
                        None,
                    )
                    print(f"Removed {args.remove} from {account_label(account)}.")
            rows = await badges_db.get_badges(connection, sonolus_id)
    held = ", ".join(f"{row.badge} (level {row.level})" for row in rows) or "none"
    print(f"Badges of {account_label(account)}: {held}")


def main() -> None:
    """Parse the command line and manage the account's badges."""
    parser = argparse.ArgumentParser(
        description=(
            "Show, give or remove an account's badges as the admin key. Without --grant or "
            "--remove it lists them. Changes are logged in staff_actions."
        )
    )
    add_config_argument(parser)
    add_constants_argument(parser)
    add_account_arguments(parser)
    change = parser.add_mutually_exclusive_group()
    grantable = sorted(get_args(GrantableBadge))
    change.add_argument("--grant", choices=grantable, help="badge to give")
    change.add_argument("--remove", choices=grantable, help="badge to take away")
    parser.add_argument("--level", type=int, default=1, help="level of a given badge (default 1)")
    args = parser.parse_args()
    if args.grant and not 1 <= args.level <= BADGES[args.grant].max_level:
        parser.error(f"--level must be between 1 and {BADGES[args.grant].max_level}")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
