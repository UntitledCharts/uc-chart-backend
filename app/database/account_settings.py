import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.database.core import Connection


class AccountSettingsRow(BaseModel):
    """An account's settings, with defaults for anything never saved.

    Attributes:
        model_config: Ignores stored keys this version doesn't know.
        collaboration_invites: Whether others may invite the account to collaborate on charts.
    """

    model_config = ConfigDict(extra="ignore")

    collaboration_invites: bool = True


async def get_settings(connection: Connection, sonolus_id: str) -> AccountSettingsRow:
    """Read an account's settings.

    Args:
        connection: Database connection.
        sonolus_id: Account id.

    Returns:
        AccountSettingsRow: The settings.
    """
    stored = await connection.fetchval(
        "SELECT settings FROM account_settings WHERE sonolus_id = $1", sonolus_id
    )
    return AccountSettingsRow.model_validate(json.loads(stored) if stored else {})


async def update_settings(
    connection: Connection, sonolus_id: str, changes: dict[str, Any]
) -> AccountSettingsRow:
    """Save some of an account's settings, keeping the others.

    Args:
        connection: Database connection.
        sonolus_id: Account id.
        changes: Settings to save.

    Returns:
        AccountSettingsRow: The settings after saving.
    """
    stored = await connection.fetchval(
        """
        INSERT INTO account_settings (sonolus_id, settings) VALUES ($1, $2::jsonb)
        ON CONFLICT (sonolus_id) DO UPDATE
        SET settings = account_settings.settings || EXCLUDED.settings, updated_at = CURRENT_TIMESTAMP
        RETURNING settings
        """,
        sonolus_id,
        json.dumps(changes),
    )
    return AccountSettingsRow.model_validate(json.loads(stored) if stored else {})
