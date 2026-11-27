import re
from dataclasses import dataclass

from app.auth.catalog import ALL_CHART_PERMISSIONS
from app.auth.principal import Principal
from app.database import charts as charts_db
from app.database import collaborators as collaborators_db
from app.database import permissions as permissions_db
from app.database.charts import ChartRow
from app.database.core import Connection
from app.errors import bad_request, not_found

CHART_ID_PATTERN = re.compile(r"^[A-Za-z0-9]{32}$")


def check_chart_id(chart_id: str) -> None:
    """Reject malformed chart ids.

    Args:
        chart_id: Chart id from the path.
    """
    if not CHART_ID_PATTERN.match(chart_id):
        raise bad_request("error.chart.invalid_id")


@dataclass
class ChartAccess:
    """A chart and what the caller may do with it.

    Attributes:
        chart: The chart.
        is_owner: Whether the caller owns it.
        chart_permissions: Collaborator permissions (all for the owner).
        principal: The caller.
    """

    chart: ChartRow
    is_owner: bool
    chart_permissions: frozenset[str]
    principal: Principal

    @property
    def can_view_hidden(self) -> bool:
        """Whether the caller sees private and deleted charts through staff power.

        Returns:
            bool: True with chart.view_hidden.
        """
        return self.principal.has_permission("chart.view_hidden")

    def has_chart_permission(self, permission: str) -> bool:
        """Check a collaborator permission.

        Args:
            permission: Chart permission name.

        Returns:
            bool: Whether the caller holds it on this chart.
        """
        return permission in self.chart_permissions

    def staff_permissions(self) -> list[str]:
        """Staff permissions the caller can use on this chart.

        Returns:
            list[str]: Permission names.
        """
        if self.principal.is_oauth:
            return []
        return sorted(
            permission
            for permission in (
                "chart.visibility",
                "chart.delete",
                "chart.restore",
                "chart.view_hidden",
                "chart.rerate",
                "chart.staff_pick",
                "comment.delete",
                "leaderboard.delete",
                "notification.send",
                "account.ban",
            )
            if self.principal.has_permission(permission)
        )


async def load_chart(
    connection: Connection,
    principal: Principal,
    chart_id: str,
    allow_hidden: bool = False,
) -> ChartAccess:
    """Load a chart the caller may see, or fail with 404.

    Args:
        connection: Database connection.
        principal: The caller.
        chart_id: Chart id.
        allow_hidden: Let callers without staff power reach soft-deleted charts they own.

    Returns:
        ChartAccess: The chart and the caller's access.
    """
    check_chart_id(chart_id)
    chart = await charts_db.get_chart(connection, chart_id, principal.sonolus_id)
    if chart is None:
        raise not_found("error.chart.not_found")
    is_owner = principal.sonolus_id is not None and principal.sonolus_id == chart.author
    if is_owner:
        chart_permissions = ALL_CHART_PERMISSIONS
    elif principal.sonolus_id is not None:
        collaborator = await collaborators_db.get_collaborator(
            connection, chart_id, principal.sonolus_id
        )
        chart_permissions = frozenset(collaborator.permissions) if collaborator else frozenset()
    else:
        chart_permissions = frozenset()
    access = ChartAccess(chart, is_owner, chart_permissions, principal)
    if chart.hidden and not access.can_view_hidden and not (allow_hidden and is_owner):
        raise not_found("error.chart.not_found")
    if chart.status == "PRIVATE":
        if is_owner or chart_permissions:
            if principal.is_oauth:
                principal.require_scope("chart:read")
        elif not access.can_view_hidden:
            raise not_found("error.chart.not_found")
    return access


async def require_outranks_owner(connection: Connection, access: ChartAccess) -> None:
    """Apply the staff hierarchy when acting on someone else's chart.

    Args:
        connection: Database connection.
        access: The chart access.
    """
    if access.is_owner:
        return
    owner_permissions = await permissions_db.get_permissions(connection, access.chart.author)
    access.principal.require_outranks(owner_permissions)
