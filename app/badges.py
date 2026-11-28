from dataclasses import dataclass
from typing import Literal

GrantableBadge = Literal["owner", "developer"]


@dataclass(frozen=True)
class BadgeDefinition:
    """A badge accounts can hold.

    Attributes:
        max_level: Highest level the badge can be granted at.
        description_key: Localization key of the badge description; clients translate it.
        images: Image file name per level, inside the public assets' badges folder.
        automatic: Whether the badge comes from staff permissions instead of being granted.
    """

    max_level: int
    description_key: str
    images: dict[int, str]
    automatic: bool = False


BADGES: dict[str, BadgeDefinition] = {
    "owner": BadgeDefinition(
        max_level=1,
        description_key="badges.owner",
        images={1: "owner-1.png"},
    ),
    "admin": BadgeDefinition(
        max_level=1,
        description_key="badges.admin",
        images={1: "admin-1.png"},
        automatic=True,
    ),
    "mod": BadgeDefinition(
        max_level=1,
        description_key="badges.mod",
        images={1: "mod-1.png"},
        automatic=True,
    ),
    "developer": BadgeDefinition(
        max_level=1,
        description_key="badges.developer",
        images={1: "developer-1.png"},
    ),
}

ADMIN_PERMISSION = "staff.manage"


def automatic_badges(permissions: frozenset[str]) -> list[str]:
    """Work out the staff badge an account's permissions give it.

    Args:
        permissions: The account's staff permissions.

    Returns:
        list[str]: ``admin`` with the staff.manage permission, ``mod`` with any other staff
            permission, otherwise nothing.
    """
    if ADMIN_PERMISSION in permissions:
        return ["admin"]
    return ["mod"] if permissions else []


def display_order(badge: str) -> int:
    """Rank a badge for display, following the catalog order.

    Args:
        badge: Badge name.

    Returns:
        int: Position in the catalog; unknown badges go last.
    """
    names = list(BADGES)
    return names.index(badge) if badge in names else len(names)


def image_asset(badge: str, level: int) -> str | None:
    """Find the public asset path of a badge level's image.

    Args:
        badge: Badge name.
        level: Badge level.

    Returns:
        str | None: Path inside the assets folder, or None for an unknown badge or level.
    """
    definition = BADGES.get(badge)
    if definition is None or level not in definition.images:
        return None
    return f"badges/{definition.images[level]}"
