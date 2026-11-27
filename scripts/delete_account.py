import argparse
import asyncio

from app.services import purge
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
    """Delete an account and everything it stored, immediately.

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
            counts = await connection.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM charts WHERE author = $1) AS charts,
                    (SELECT COUNT(*) FROM leaderboards WHERE submitter = $1) AS records,
                    (SELECT COUNT(*) FROM comments WHERE commenter = $1) AS comments
                """,
                account.sonolus_id,
            )
        print(f"Account: {account_label(account)}")
        print(
            f"  {counts['charts']} chart(s), {counts['records']} leaderboard record(s), "
            f"{counts['comments']} comment(s)"
        )
        print(
            "This permanently deletes the account, every file it stored and every row "
            "referencing it, right now. It can't be rolled back."
        )
        if not args.yes and not confirm("Type 'yes' to continue:", "yes"):
            print("Aborted.")
            return
        existed = await purge.purge_account(context, account.sonolus_id)
        if existed:
            async with context.database.connection() as connection:
                await record_admin_action(
                    context,
                    connection,
                    "account_delete",
                    "account",
                    account.sonolus_id,
                    f"{account.sonolus_username}#{account.sonolus_handle}",
                    None,
                )
    if existed:
        print(f"Deleted {account_label(account)} and its files.")
    else:
        print(
            f"Deleted the files of {account_label(account)}; its row was already gone, "
            "so nothing was logged."
        )


def main() -> None:
    """Parse the command line and delete the account."""
    parser = argparse.ArgumentParser(
        description=(
            "Delete an account, its files and its rows immediately, as the admin key "
            "(same as DELETE /v1/accounts/{id}). Logged in staff_actions as account_delete."
        )
    )
    add_config_argument(parser)
    add_constants_argument(parser)
    add_account_arguments(parser)
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
