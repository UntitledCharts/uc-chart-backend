from app.database import badges as badges_db
from app.database import permissions as permissions_db
from app.database.core import Connection
from app.models import present, schemas
from app.services.storage import Storage


async def load(
    connection: Connection, sonolus_ids: list[str], storage: Storage
) -> present.AccountTags:
    """Fetch the staff permissions and badges of the accounts shown next to some content.

    Args:
        connection: Database connection.
        sonolus_ids: Account ids.
        storage: Storage client, for badge images.

    Returns:
        present.AccountTags: Permissions and badges keyed by account id.
    """
    permissions = await permissions_db.get_permissions_for(connection, sonolus_ids)
    badges = await badges_db.get_badges_for(connection, sonolus_ids)
    return present.AccountTags(
        permissions=permissions,
        badges={
            sonolus_id: present.account_badges(
                badges.get(sonolus_id, []),
                permissions.get(sonolus_id, frozenset()),
                storage,
            )
            for sonolus_id in set(sonolus_ids)
        },
    )


async def load_badges(
    connection: Connection,
    sonolus_id: str,
    permissions: frozenset[str],
    storage: Storage,
) -> list[schemas.AccountBadge]:
    """Fetch one account's badges.

    Args:
        connection: Database connection.
        sonolus_id: Account id.
        permissions: The account's staff permissions, which can give it a staff badge.
        storage: Storage client, for badge images.

    Returns:
        list[schemas.AccountBadge]: The badges.
    """
    rows = await badges_db.get_badges(connection, sonolus_id)
    return present.account_badges(rows, permissions, storage)
