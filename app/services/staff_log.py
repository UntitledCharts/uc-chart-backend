from app.auth.principal import Principal
from app.context import AppContext
from app.database import staff_actions as staff_actions_db
from app.database.core import Connection


def actor_label(principal: Principal) -> str:
    """Describe who acted, for logs and Discord.

    Args:
        principal: The acting caller.

    Returns:
        str: ``name#handle`` or ``admin key``.
    """
    if principal.is_admin_key or principal.account is None:
        return "admin key"
    return f"{principal.account.sonolus_username}#{principal.account.sonolus_handle}"


async def record(
    context: AppContext,
    connection: Connection,
    principal: Principal,
    action: str,
    target_type: str,
    target_id: str,
    previous_value: str | None,
    new_value: str | None,
) -> None:
    """Log a staff action and announce it on Discord.

    Args:
        context: App context.
        connection: Database connection, ideally the transaction making the change.
        principal: The acting caller.
        action: Action name.
        target_type: Kind of target.
        target_id: Target id.
        previous_value: Value before the action, used for rollback.
        new_value: Value after the action.
    """
    await staff_actions_db.log_action(
        connection,
        principal.sonolus_id,
        principal.is_admin_key,
        action,
        target_type,
        target_id,
        previous_value,
        new_value,
    )
    context.discord.staff_action(
        actor_label(principal),
        action,
        target_type,
        target_id,
        previous_value,
        new_value,
    )
