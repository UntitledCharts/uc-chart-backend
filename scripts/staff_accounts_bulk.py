import argparse
import asyncio
from pathlib import Path

from app.database import accounts as accounts_db
from scripts._common import (
    add_config_argument,
    add_constants_argument,
    open_context,
    read_config,
    read_constants,
)
from scripts.staff_account import (
    PermissionPlan,
    add_plan_arguments,
    apply_plan,
    describe_changes,
    plan_from_args,
)


def read_handles(parser: argparse.ArgumentParser, args: argparse.Namespace) -> list[int]:
    """Collect handles from the command line and the optional file, without duplicates.

    Args:
        parser: Parser used to report bad input.
        args: Parsed command line.

    Returns:
        list[int]: Handles in the order given.
    """
    handles: list[int] = list(args.handles)
    if args.file:
        for word in Path(args.file).read_text(encoding="utf-8").split():
            try:
                handles.append(int(word.lstrip("#")))
            except ValueError:
                parser.error(f"{args.file}: not a handle: {word}")
    if not handles:
        parser.error("give at least one handle, on the command line or with --file")
    return list(dict.fromkeys(handles))


async def run(args: argparse.Namespace, handles: list[int], plan: PermissionPlan) -> None:
    """Apply the same permission changes to several accounts.

    Args:
        args: Parsed command line.
        handles: Sonolus handles to change.
        plan: Changes to make.
    """
    config = read_config(args.config)
    constants = read_constants(args.constants)
    print(f"{len(handles)} account(s): {plan.describe()}")
    failed = 0
    async with open_context(config, constants) as context:
        for handle in handles:
            async with context.database.transaction() as connection:
                account = await accounts_db.get_account_by_handle(connection, handle)
                if account is None:
                    print(f"[{handle}] failed to resolve: no account with this handle")
                    failed += 1
                    continue
                granted, revoked = await apply_plan(context, connection, account.sonolus_id, plan)
            print(
                f"[{handle}] {account.sonolus_id} {account.sonolus_username}: "
                f"{describe_changes(granted, revoked)}"
            )
    if failed:
        raise SystemExit(f"{failed} handle(s) could not be resolved")


def main() -> None:
    """Parse the command line and change the accounts' staff permissions."""
    parser = argparse.ArgumentParser(
        description=(
            "Grant or revoke staff permissions on several accounts by Sonolus handle, as the "
            "admin key. Each grant and revoke is logged in staff_actions."
        )
    )
    add_config_argument(parser)
    add_constants_argument(parser)
    parser.add_argument("handles", nargs="*", type=int, help="Sonolus handles")
    parser.add_argument("--file", help="file with more handles, separated by whitespace")
    add_plan_arguments(parser)
    args = parser.parse_args()
    plan = plan_from_args(parser, args)
    if plan.empty:
        parser.error("nothing to do: pass --grant, --revoke, --preset or --revoke-all")
    asyncio.run(run(args, read_handles(parser, args), plan))


if __name__ == "__main__":
    main()
