import argparse
import asyncio

from app.database import accounts as accounts_db
from app.database import oauth as oauth_db
from app.database import sessions as sessions_db
from scripts._common import (
    account_label,
    add_account_arguments,
    add_config_argument,
    add_constants_argument,
    confirm,
    describe_target,
    find_account,
    open_context,
    read_config,
    read_constants,
    record_admin_action,
)


async def run(args: argparse.Namespace) -> None:
    """Ban or unban an account directly in the database.

    Args:
        args: Parsed command line.
    """
    config = read_config(args.config)
    constants = read_constants(args.constants)
    async with open_context(config, constants) as context:
        async with context.database.connection() as connection:
            account = await find_account(connection, args.account_id, args.handle)
        if account is None:
            raise SystemExit(
                f"No account found for {describe_target(args.account_id, args.handle)}."
            )
        sonolus_id = account.sonolus_id
        state = "banned" if account.banned else "not banned"
        if account.deleted_at is not None:
            state += f", pending deletion since {account.deleted_at}"
        print(f"Account: {account_label(account)} ({state})")
        grace_days = constants.charts.deletion_grace_days
        if args.unban:
            print("This unbans it and cancels its pending deletion.")
        elif args.no_delete:
            print(
                "This bans it and ends its sessions and app access, without scheduling a deletion "
                "(a deletion that is already pending stays pending)."
            )
        else:
            print(
                "This bans it, ends its sessions and app access, and schedules its content and "
                f"files for deletion in {grace_days} days unless it is unbanned. The banned "
                "account row itself is kept so the ban survives a new login."
            )
        if not args.yes and not confirm("Type 'yes' to continue:", "yes"):
            print("Aborted.")
            return

        async with context.database.transaction() as connection:
            if args.unban:
                previous = await accounts_db.set_banned(connection, sonolus_id, False)
                await accounts_db.set_pending_deletion(connection, sonolus_id, False)
                await record_admin_action(
                    context, connection, "ban", "account", sonolus_id, str(previous), "False"
                )
                ended = 0
            else:
                previous = await accounts_db.set_banned(connection, sonolus_id, True)
                if not args.no_delete:
                    await accounts_db.set_pending_deletion(connection, sonolus_id, True)
                ended = await sessions_db.delete_all_sessions(connection, sonolus_id)
                await oauth_db.revoke_all_for_account(connection, sonolus_id)
                await record_admin_action(
                    context, connection, "ban", "account", sonolus_id, str(previous), "True"
                )
    if args.unban:
        print(f"Unbanned {account_label(account)} (was banned: {previous}).")
    else:
        print(
            f"Banned {account_label(account)} (was banned: {previous}); "
            f"ended {ended} session(s) and revoked its OAuth tokens."
        )


def main() -> None:
    """Parse the command line and ban or unban the account."""
    parser = argparse.ArgumentParser(
        description=(
            "Ban or unban an account as the admin key: bans end every session and OAuth token, "
            "and by default schedule the account for deletion. Logged in staff_actions."
        )
    )
    add_config_argument(parser)
    add_constants_argument(parser)
    add_account_arguments(parser)
    parser.add_argument("--unban", action="store_true", help="unban and cancel pending deletion")
    parser.add_argument(
        "--no-delete",
        action="store_true",
        help="ban without scheduling the account for deletion (keeps a pending deletion)",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()
    if args.unban and args.no_delete:
        parser.error("--no-delete only applies to bans")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
