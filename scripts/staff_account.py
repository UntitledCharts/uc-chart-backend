import argparse
import asyncio
from dataclasses import dataclass

from app.auth.catalog import ADMIN_PERMISSIONS, ALL_STAFF_PERMISSIONS, MOD_PERMISSIONS
from app.context import AppContext
from app.database import permissions as permissions_db
from app.database.core import Connection
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

PRESETS: dict[str, frozenset[str]] = {
    "mod": MOD_PERMISSIONS,
    "admin": ADMIN_PERMISSIONS,
}


@dataclass(frozen=True)
class PermissionPlan:
    """Staff permission changes requested on the command line.

    Attributes:
        grant: Permissions to grant.
        revoke: Permissions to revoke.
        revoke_all: Whether every permission not being granted is revoked.
    """

    grant: frozenset[str]
    revoke: frozenset[str]
    revoke_all: bool

    @property
    def empty(self) -> bool:
        """Whether the plan changes nothing.

        Returns:
            bool: True when no change was requested.
        """
        return not self.grant and not self.revoke and not self.revoke_all

    def describe(self) -> str:
        """Summarize the plan.

        Returns:
            str: Human-readable summary.
        """
        parts = []
        if self.revoke_all:
            parts.append("revoke all")
        if self.revoke:
            parts.append("revoke " + ", ".join(sorted(self.revoke)))
        if self.grant:
            parts.append("grant " + ", ".join(sorted(self.grant)))
        return "; ".join(parts) or "no change"


def add_plan_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the permission change options to a parser.

    Args:
        parser: Parser to extend.
    """
    choices = sorted(ALL_STAFF_PERMISSIONS)
    parser.add_argument(
        "--grant",
        action="append",
        default=[],
        choices=choices,
        metavar="PERMISSION",
        help=f"grant a permission, repeatable; one of: {', '.join(choices)}",
    )
    parser.add_argument(
        "--revoke",
        action="append",
        default=[],
        choices=choices,
        metavar="PERMISSION",
        help="revoke a permission, repeatable",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        help="grant the mod set, or every permission for admin",
    )
    parser.add_argument(
        "--revoke-all",
        action="store_true",
        help="revoke every permission (combined with --grant/--preset: keep only those)",
    )


def plan_from_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> PermissionPlan:
    """Build the permission plan from parsed options.

    Args:
        parser: Parser used to report conflicting options.
        args: Parsed command line.

    Returns:
        PermissionPlan: The plan.
    """
    grant = set(args.grant)
    if args.preset:
        grant |= PRESETS[args.preset]
    revoke = set(args.revoke)
    conflicts = grant & revoke
    if conflicts:
        parser.error(f"granted and revoked at once: {', '.join(sorted(conflicts))}")
    return PermissionPlan(frozenset(grant), frozenset(revoke), args.revoke_all)


async def apply_plan(
    context: AppContext, connection: Connection, sonolus_id: str, plan: PermissionPlan
) -> tuple[list[str], list[str]]:
    """Apply a plan to one account, logging each real change as the admin key.

    Args:
        context: App context.
        connection: Database connection inside a transaction.
        sonolus_id: Account id.
        plan: Changes to make.

    Returns:
        tuple[list[str], list[str]]: Permissions granted and permissions revoked.
    """
    current = await permissions_db.get_permissions(connection, sonolus_id)
    removed = current if plan.revoke_all else current & plan.revoke
    target = (current - removed) | plan.grant
    granted: list[str] = []
    revoked: list[str] = []
    for permission in sorted(current - target):
        if await permissions_db.revoke_permission(connection, sonolus_id, permission):
            await record_admin_action(
                context,
                connection,
                "permission_revoke",
                "account",
                sonolus_id,
                permission,
                None,
            )
            revoked.append(permission)
    for permission in sorted(target - current):
        if await permissions_db.grant_permission(connection, sonolus_id, permission, None):
            await record_admin_action(
                context,
                connection,
                "permission_grant",
                "account",
                sonolus_id,
                None,
                permission,
            )
            granted.append(permission)
    return granted, revoked


def describe_changes(granted: list[str], revoked: list[str]) -> str:
    """Summarize applied changes.

    Args:
        granted: Permissions granted.
        revoked: Permissions revoked.

    Returns:
        str: Text like ``+a +b -c`` or ``no change``.
    """
    changes = [f"+{permission}" for permission in granted]
    changes += [f"-{permission}" for permission in revoked]
    return " ".join(changes) or "no change"


async def run(args: argparse.Namespace, plan: PermissionPlan) -> None:
    """Show or change one account's staff permissions.

    Args:
        args: Parsed command line.
        plan: Changes to make.
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
            print(f"Account: {account_label(account)}")
            if not plan.empty:
                granted, revoked = await apply_plan(context, connection, account.sonolus_id, plan)
                print(f"Changes: {describe_changes(granted, revoked)}")
            grants = await permissions_db.list_grants(connection, account.sonolus_id)
    if not grants:
        print("Permissions: none")
        return
    print("Permissions:")
    for grant in grants:
        granted_by = grant.granted_by or "admin key"
        print(f"  {grant.permission:<20} granted {grant.granted_at:%Y-%m-%d %H:%M} by {granted_by}")


def main() -> None:
    """Parse the command line and manage the account's staff permissions."""
    parser = argparse.ArgumentParser(
        description=(
            "Show or change an account's staff permissions as the admin key. Without a change "
            "option it lists them. Each grant and revoke is logged in staff_actions."
        )
    )
    add_config_argument(parser)
    add_constants_argument(parser)
    add_account_arguments(parser)
    add_plan_arguments(parser)
    args = parser.parse_args()
    asyncio.run(run(args, plan_from_args(parser, args)))


if __name__ == "__main__":
    main()
