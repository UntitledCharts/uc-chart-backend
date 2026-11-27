from typing import Literal

StaffPermission = Literal[
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
    "account.delete",
    "staff.manage",
]

MOD_PERMISSIONS: frozenset[str] = frozenset(
    {
        "chart.visibility",
        "chart.delete",
        "chart.restore",
        "chart.view_hidden",
        "chart.rerate",
        "chart.staff_pick",
        "comment.delete",
        "leaderboard.delete",
        "notification.send",
    }
)

ADMIN_PERMISSIONS: frozenset[str] = MOD_PERMISSIONS | frozenset(
    {"account.ban", "account.delete", "staff.manage"}
)

ALL_STAFF_PERMISSIONS: frozenset[str] = ADMIN_PERMISSIONS

ChartPermission = Literal["edit.files", "edit.metadata", "visibility", "credit", "manage"]

ALL_CHART_PERMISSIONS: frozenset[str] = frozenset(
    {"edit.files", "edit.metadata", "visibility", "credit", "manage"}
)

OAuthScope = Literal[
    "chart:read",
    "chart:upload",
    "chart:edit",
    "chart:metadata",
    "chart:visibility",
    "chart:delete",
    "chart:like",
    "chart:collaborate",
    "comment:write",
    "leaderboard:delete",
    "notifications:read",
    "notifications:write",
    "user:read",
    "user:edit",
]

SCOPE_DESCRIPTIONS: dict[str, str] = {
    "chart:read": "List and open all of your charts, including the unlisted and private ones.",
    "chart:upload": "Upload new charts as you.",
    "chart:edit": "Replace the files (chart, audio, jacket, background, preview) of your charts.",
    "chart:metadata": "Edit the metadata (title, artists, tags, rating, description) of your charts.",
    "chart:visibility": "Publish, unlist, or hide your charts, and schedule them to publish.",
    "chart:delete": "Delete your charts.",
    "chart:like": "Like and unlike charts as you.",
    "chart:collaborate": "Manage chart collaborators and answer collaboration invites as you.",
    "comment:write": "Post and delete your comments.",
    "leaderboard:delete": "Delete your leaderboard records.",
    "notifications:read": "Read your notifications.",
    "notifications:write": "Mark your notifications read or unread and delete them.",
    "user:read": "Read your account information.",
    "user:edit": "Change your avatar, banner and profile description.",
}

ALL_SCOPES: tuple[str, ...] = tuple(SCOPE_DESCRIPTIONS)


def parse_scopes(scope: str | None) -> list[str]:
    """Split a space-separated scope string, keeping known scopes only.

    Args:
        scope: Scope string from an OAuth request.

    Returns:
        list[str]: Known scopes in request order without duplicates.
    """
    if not scope:
        return []
    return list(dict.fromkeys(item for item in scope.split(" ") if item in SCOPE_DESCRIPTIONS))
