from datetime import datetime, timedelta, timezone

from typing import Optional, Literal

from database.query import ExecutableQuery, SelectQuery
from helpers.models import (
    OAuth,
    PublicAccount,
    SessionData,
    Account,
    Notification,
    NotificationList,
    Count,
    UserStats,
)

"""
sonolus_sessions JSONB

{
    "game": {
        "1": {"session_key": "same as key", "expires": epoch in ms},
        "2": ...,
        "3": ...
    },
    "external": {
        same as above
    }
}
"""

"""
oauth_details JSONB

{
    "service_name": {
        "access_token": "",
        "refresh_token": "",
        "expires_at": 0
    }
}
"""


def add_oauth(
    sonolus_id: str,
    oauth: OAuth,
    service: Literal["discord"],
) -> ExecutableQuery:
    assert service in ["discord"]

    return ExecutableQuery(
        f"""
            UPDATE accounts
            SET oauth_details = jsonb_set(
                COALESCE(oauth_details, '{{}}'::jsonb),
                '{{{service}}}',
                to_jsonb($2::jsonb)
            )
            WHERE sonolus_id = $1;
        """,
        sonolus_id,
        oauth.model_dump(),
    )


def delete_oauth(sonolus_id: str, service: Literal["discord"]) -> ExecutableQuery:
    assert service in ["discord"]

    return ExecutableQuery(
        f"""
            UPDATE accounts
            SET oauth_details = oauth_details - '{service}'
            WHERE sonolus_id = $1;
        """,
        sonolus_id,
    )


def generate_get_oauth_query(
    sonolus_id: str, service: Literal["discord"]
) -> SelectQuery[OAuth]:
    assert service in ["discord"]

    return SelectQuery(
        OAuth,
        f"""
            SELECT oauth_details->'{service}'
            FROM accounts
            WHERE sonolus_id = $1;
        """,
        sonolus_id,
    )


def generate_create_account_query(
    sonolus_id: str, sonolus_handle: int, sonolus_username: str
) -> ExecutableQuery:
    return ExecutableQuery(
        """
            INSERT INTO accounts (sonolus_id, sonolus_handle, sonolus_username)
            VALUES ($1, $2, $3);
        """,
        sonolus_id,
        sonolus_handle,
        sonolus_username,
    )


def create_account_if_not_exists_and_new_session(
    session_key: str,
    sonolus_id: str,
    sonolus_handle: int,
    sonolus_username: str,
    session_type: str,
    expiry_ms: int = 30 * 60 * 1000,
) -> tuple[ExecutableQuery, SelectQuery[SessionData]]:
    """
    Create or update an account, then create a new session slot.
    Returns two SelectQuery objects:
      1. Upsert account (always updates username/handle)
      2. Update session slot and return session_key & expires
    """
    if session_type not in ("game", "external"):
        raise ValueError("invalid session type. must be 'game' or 'external'.")

    expiry_time = int(
        (datetime.now(timezone.utc) + timedelta(milliseconds=expiry_ms)).timestamp()
        * 1000
    )

    upsert_query = ExecutableQuery(
        f"""
        INSERT INTO accounts (sonolus_id, sonolus_handle, sonolus_username, sonolus_sessions)
        VALUES ($1, $2, $3, jsonb_build_object('game','{{}}'::jsonb,'external','{{}}'::jsonb))
        ON CONFLICT (sonolus_id) DO UPDATE
        SET sonolus_username = EXCLUDED.sonolus_username;
        """,
        sonolus_id,
        sonolus_handle,
        sonolus_username,
    )

    session_query = SelectQuery(
        SessionData,
        f"""
        WITH slot_to_use AS (
            SELECT
                CASE
                    WHEN jsonb_extract_path(sonolus_sessions, $2, '1') IS NULL OR
                        (jsonb_extract_path(sonolus_sessions, $2, '1')->>'expires')::bigint < extract(epoch from now())*1000
                    THEN '1'
                    WHEN jsonb_extract_path(sonolus_sessions, $2, '2') IS NULL OR
                        (jsonb_extract_path(sonolus_sessions, $2, '2')->>'expires')::bigint < extract(epoch from now())*1000
                    THEN '2'
                    WHEN jsonb_extract_path(sonolus_sessions, $2, '3') IS NULL OR
                        (jsonb_extract_path(sonolus_sessions, $2, '3')->>'expires')::bigint < extract(epoch from now())*1000
                    THEN '3'
                    ELSE (
                        SELECT key
                        FROM jsonb_each(jsonb_extract_path(sonolus_sessions, $2)) AS t(key,val)
                        ORDER BY (val->>'expires')::bigint ASC
                        LIMIT 1
                    )
                END AS slot
            FROM accounts
            WHERE sonolus_id=$1
        )
        UPDATE accounts a
        SET sonolus_sessions = jsonb_set(
            a.sonolus_sessions,
            array[$2, s.slot],
            jsonb_build_object(
                'session_key', $3::text,
                'expires', $4::bigint
            ),
            true
        )
        FROM slot_to_use s
        WHERE a.sonolus_id=$1
        RETURNING 
            (jsonb_extract_path(a.sonolus_sessions, $2, s.slot)->>'session_key')::text AS session_key,
            (jsonb_extract_path(a.sonolus_sessions, $2, s.slot)->>'expires')::bigint AS expires;
        """,
        sonolus_id,
        session_type,
        session_key,
        expiry_time,
    )
    return upsert_query, session_query


def get_account_from_handle(handle: int) -> SelectQuery[Account]:
    return SelectQuery(
        Account,
        f"""
            SELECT *
            FROM accounts
            WHERE sonolus_handle = $1
            LIMIT 1;
        """,
        handle,
    )


def get_account_from_session(
    sonolus_id: str, session_key: str, session_type: str
) -> SelectQuery[Account]:
    assert session_type in ["game", "external"]

    return SelectQuery(
        Account,
        f"""
            SELECT *
            FROM accounts
            WHERE sonolus_id = $1
            AND EXISTS (
                SELECT 1
                FROM jsonb_each(COALESCE(sonolus_sessions->'{session_type}', '{{}}'::jsonb)) AS sessions(slot, data)
                WHERE data->>'session_key' = $2::text
                    AND (data->>'expires')::bigint > EXTRACT(EPOCH FROM NOW()) * 1000
            )
            LIMIT 1;
        """,
        sonolus_id,
        session_key,
    )


def get_public_account(sonolus_id: str) -> SelectQuery[PublicAccount]:
    return SelectQuery(
        PublicAccount,
        """
            SELECT
                sonolus_id,
                sonolus_handle,
                sonolus_username,
                profile_hash,
                banner_hash,
                description,
                mod,
                admin,
                banned
            FROM accounts
            WHERE sonolus_id = $1
            LIMIT 1;
        """,
        sonolus_id,
    )


def get_public_account_batch(sonolus_ids: list[str]) -> SelectQuery[PublicAccount]:
    return SelectQuery(
        PublicAccount,
        """
            SELECT
                sonolus_id,
                sonolus_handle,
                sonolus_username,
                profile_hash,
                banner_hash,
                description,
                mod,
                admin,
                banned
            FROM accounts
            WHERE sonolus_id = ANY($1::text[]);
        """,
        sonolus_ids,
    )


def update_cooldown(sonolus_id: str, time_to_add: timedelta) -> ExecutableQuery:
    cooldown_until = datetime.now(timezone.utc) + time_to_add

    return ExecutableQuery(
        """
            UPDATE accounts
            SET chart_upload_cooldown = $1
            WHERE sonolus_id = $2
        """,
        cooldown_until,
        sonolus_id,
    )


def delete_account(sonolus_id: str, confirm_change: bool = False) -> ExecutableQuery:
    if not confirm_change:
        raise ValueError(
            "Deletion not confirmed. Ensure you are deleting the old chart files from S3 to ensure there is no hanging files."
        )

    return ExecutableQuery(
        """
            DELETE FROM accounts
            WHERE sonolus_id = $1;
        """,
        sonolus_id,
    )


def link_discord_id(sonolus_id: str, discord_id: int) -> ExecutableQuery:
    return ExecutableQuery(
        """
            UPDATE accounts
            SET discord_id = $1, updated_at = CURRENT_TIMESTAMP
            WHERE sonolus_id = $2;
        """,
        discord_id,
        sonolus_id,
    )


def link_patreon_id(  # Merge into one function with link_discord_id?
    sonolus_id: str, patreon_id: str
) -> ExecutableQuery:
    return ExecutableQuery(
        """
            UPDATE accounts
            SET patreon_id = $1, updated_at = CURRENT_TIMESTAMP
            WHERE sonolus_id = $2;
        """,
        patreon_id,
        sonolus_id,
    )


def set_admin(sonolus_id: str, admin_status: bool) -> ExecutableQuery:
    if admin_status == False:
        return ExecutableQuery(
            """
                UPDATE accounts
                SET admin = $1, updated_at = CURRENT_TIMESTAMP
                WHERE sonolus_id = $2;
            """,
            admin_status,
            sonolus_id,
        )
    elif admin_status == True:
        return ExecutableQuery(
            """
                UPDATE accounts
                SET admin = $1, mod = $1, updated_at = CURRENT_TIMESTAMP
                WHERE sonolus_id = $2;
            """,
            admin_status,
            sonolus_id,
        )


def set_mod(sonolus_id: str, mod_status: bool) -> ExecutableQuery:
    if mod_status == True:
        return ExecutableQuery(
            """
                UPDATE accounts
                SET mod = $1, updated_at = CURRENT_TIMESTAMP
                WHERE sonolus_id = $2;
            """,
            mod_status,
            sonolus_id,
        )
    elif mod_status == False:
        return ExecutableQuery(
            """
                UPDATE accounts
                SET mod = $1, admin = $1, updated_at = CURRENT_TIMESTAMP
                WHERE sonolus_id = $2;
            """,
            mod_status,
            sonolus_id,
        )


def set_banned(sonolus_id: str, banned_status: bool) -> ExecutableQuery:
    return ExecutableQuery(
        """
            UPDATE accounts
            SET banned = $1, updated_at = CURRENT_TIMESTAMP
            WHERE sonolus_id = $2;
        """,
        banned_status,
        sonolus_id,
    )


def update_chart_upload_cooldown(
    sonolus_id: str, cooldown_timestamp: str
) -> ExecutableQuery:
    return ExecutableQuery(
        """
            UPDATE accounts
            SET chart_upload_cooldown = $1, updated_at = CURRENT_TIMESTAMP
            WHERE sonolus_id = $2;
        """,
        cooldown_timestamp,
        sonolus_id,
    )


def get_unread_notifications_count(sonolus_id: str) -> SelectQuery[Count]:
    return SelectQuery(
        Count,
        """
            SELECT COUNT(*) AS total_count
            FROM notifications
            WHERE user_id = $1
            AND is_read = false;
        """,
        sonolus_id,
    )


def get_notifications(
    sonolus_id: str,
    limit: int = 10,
    page: int = 0,
    only_unread: bool = False,
) -> SelectQuery[NotificationList]:
    if only_unread:
        return SelectQuery(
            NotificationList,
            """
                SELECT
                    id,
                    title,
                    is_read,
                    created_at
                FROM notifications
                WHERE user_id = $1 AND is_read = false
                ORDER BY created_at DESC
                LIMIT $2 OFFSET $3;
            """,
            sonolus_id,
            limit,
            page * limit,
        )
    return SelectQuery(
        NotificationList,
        """
            SELECT
                id,
                title,
                is_read,
                created_at
            FROM notifications
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3;
        """,
        sonolus_id,
        limit,
        page * limit,
    )


def get_notification(id: str, sonolus_id: str) -> SelectQuery[Notification]:
    return SelectQuery(
        Notification,
        """
            UPDATE notifications
            SET is_read = true
            WHERE id = $1 AND user_id = $2
            RETURNING id, user_id, title, content, is_read, created_at;
        """,
        id,
        sonolus_id,
    )


def delete_notification(id: str, sonolus_id: str) -> SelectQuery[Notification]:
    return SelectQuery(
        Notification,
        """
            DELETE FROM notifications
            WHERE id = $1 AND user_id = $2
            RETURNING *;
        """,
        id,
        sonolus_id,
    )


def add_notification(sonolus_id: str, title: str, content: str) -> ExecutableQuery:
    return ExecutableQuery(
        """
            INSERT INTO notifications (user_id, title, content)
            VALUES ($1, $2, $3);
        """,
        sonolus_id,
        title,
        content,
    )


def toggle_notification_read_status(
    notification_id: str, user_id: str, is_read: bool
) -> SelectQuery[Notification]:
    return SelectQuery(
        Notification,
        """
            UPDATE notifications
            SET is_read = $3
            WHERE id = $1 AND user_id = $2
            RETURNING id, user_id, title, content, is_read, created_at;
        """,
        notification_id,
        user_id,
        is_read,
    )


def get_account_stats(sonolus_id: str) -> SelectQuery[UserStats]:
    return SelectQuery(
        UserStats,
        """
        SELECT
            a.sonolus_id,
            a.sonolus_handle,

            -- interaction stats
            (
                SELECT COUNT(*)
                FROM chart_likes cl
                WHERE cl.sonolus_id = a.sonolus_id
            ) AS liked_charts_count,

            (
                SELECT COUNT(*)
                FROM comments c
                WHERE c.commenter = a.sonolus_id
            ) AS comments_count,

            -- chart stats
            (
                SELECT COUNT(*)
                FROM charts ch
                WHERE ch.author = a.sonolus_id
                AND ch.status = 'PUBLIC'
            ) AS charts_published,

            (
                SELECT COUNT(*)
                FROM chart_likes cl
                JOIN charts ch ON ch.id = cl.chart_id
                WHERE ch.author = a.sonolus_id
            ) AS likes_received,

            (
                SELECT COUNT(*)
                FROM comments c
                JOIN charts ch ON ch.id = c.chart_id
                WHERE ch.author = a.sonolus_id
            ) AS comments_received
        FROM accounts a
        WHERE a.sonolus_id = $1
        """,
        sonolus_id,
    )


def update_description(sonolus_id: str, description: Optional[str]) -> ExecutableQuery:
    return ExecutableQuery(
        "UPDATE accounts SET description = $1 WHERE sonolus_id = $2",
        description,
        sonolus_id,
    )


def update_profile_hash(
    sonolus_id: str, profile_hash: Optional[str]
) -> ExecutableQuery:
    return ExecutableQuery(
        "UPDATE accounts SET profile_hash = $1 WHERE sonolus_id = $2",
        profile_hash,
        sonolus_id,
    )


def update_banner_hash(sonolus_id: str, banner_hash: Optional[str]) -> ExecutableQuery:
    return ExecutableQuery(
        "UPDATE accounts SET banner_hash = $1 WHERE sonolus_id = $2",
        banner_hash,
        sonolus_id,
    )
