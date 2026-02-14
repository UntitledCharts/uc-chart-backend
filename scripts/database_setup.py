import asyncio

import asyncpg
import yaml

with open("config.yml", "r") as f:
    config = yaml.load(f, yaml.Loader)

psql_config = config["psql"]


async def main():
    db = await asyncpg.create_pool(
        host=psql_config["host"],
        user=psql_config["user"],
        database=psql_config["database"],
        password=psql_config["password"],
        port=psql_config["port"],
        min_size=psql_config["pool-min-size"],
        max_size=psql_config["pool-max-size"],
        ssl="disable",
    )
    print("Connected!")
    # uncomment first block ONLY to delete all tables.
    # should not ever be run for production
    queries = [
        # """DO $$
        # DECLARE
        #     r RECORD;
        # BEGIN
        #     -- Iterate over each table and drop it
        #     FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
        #         EXECUTE 'DROP TABLE IF EXISTS public.' || r.tablename || ' CASCADE';
        #     END LOOP;
        # END $$;
        # """,
        """CREATE EXTENSION IF NOT EXISTS pg_trgm;""",
        # Commented: requires super user!
        # """CREATE EXTENSION IF NOT EXISTS pg_cron;""",
        """DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'chart_status') THEN
        CREATE TYPE chart_status AS ENUM ('UNLISTED', 'PRIVATE', 'PUBLIC');
    END IF;
END $$;""",
        """CREATE TABLE IF NOT EXISTS accounts (
    sonolus_id TEXT PRIMARY KEY,
    sonolus_handle BIGINT NOT NULL,
    sonolus_username TEXT NOT NULL,
    discord_id BIGINT,
    patreon_id TEXT,
    chart_upload_cooldown TIMESTAMP with time zone,
    sonolus_sessions JSONB,
    oauth_details JSONB,
    subscription_details JSONB,
    created_at timestamp with time zone DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    updated_at timestamp with time zone DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    description TEXT,
    profile_hash TEXT,
    banner_hash TEXT,
    mod BOOL DEFAULT false,
    admin BOOL default false,
    banned BOOL DEFAULT false
);""",
        """CREATE TABLE IF NOT EXISTS charts (
    id TEXT PRIMARY KEY,
    rating DECIMAL DEFAULT 1,
    staff_pick BOOL DEFAULT FALSE,
    sponsored_until TIMESTAMP DEFAULT NULL,
    author TEXT REFERENCES accounts(sonolus_id) ON DELETE CASCADE,
    chart_author TEXT NOT NULL,
    description TEXT,
    title TEXT NOT NULL,
    artists TEXT,
    tags TEXT[] DEFAULT '{}',
    like_count BIGINT NOT NULL DEFAULT 0,
    comment_count BIGINT NOT NULL DEFAULT 0,
    log_like_score DOUBLE PRECISION DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    updated_at timestamp with time zone DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    published_at timestamp with time zone DEFAULT NULL,
    status chart_status NOT NULL,
    jacket_file_hash TEXT NOT NULL,
    music_file_hash TEXT NOT NULL,
    chart_file_hash TEXT NOT NULL,
    preview_file_hash TEXT,
    background_file_hash TEXT,
    background_v1_file_hash TEXT NOT NULL,
    background_v3_file_hash TEXT NOT NULL,
    scheduled_publish TIMESTAMPTZ DEFAULT NULL
);""",
        """CREATE TABLE IF NOT EXISTS chart_likes (
    chart_id TEXT NOT NULL REFERENCES charts(id) ON DELETE CASCADE,
    sonolus_id TEXT NOT NULL REFERENCES accounts(sonolus_id) ON DELETE CASCADE,
    created_at timestamp with time zone DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    PRIMARY KEY (chart_id, sonolus_id)
);""",
        """CREATE TABLE IF NOT EXISTS comments (
    id SERIAL PRIMARY KEY,
    commenter TEXT REFERENCES accounts(sonolus_id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    created_at timestamp with time zone DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC'),
    deleted_at timestamp with time zone DEFAULT (NULL AT TIME ZONE 'UTC'),
    chart_id TEXT REFERENCES charts(id) ON DELETE CASCADE
);""",
        """CREATE TABLE IF NOT EXISTS notifications (
    id SERIAL PRIMARY KEY,
    user_id TEXT REFERENCES accounts(sonolus_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    content TEXT,
    is_read BOOL DEFAULT false,
    created_at timestamp with time zone DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
);""",
        """CREATE OR REPLACE FUNCTION update_comment_count()
RETURNS TRIGGER AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM charts WHERE id = COALESCE(NEW.chart_id, OLD.chart_id)) THEN

        IF TG_OP = 'INSERT' THEN
            -- increment comment_count
            UPDATE charts
            SET comment_count = comment_count + 1
            WHERE id = NEW.chart_id;

        ELSIF TG_OP = 'DELETE' THEN
            -- decrement comment_count
            UPDATE charts
            SET comment_count = comment_count - 1
            WHERE id = OLD.chart_id;

        END IF;

    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_update_comment_count ON comments;

CREATE TRIGGER trg_update_comment_count
AFTER INSERT OR DELETE ON comments
FOR EACH ROW
EXECUTE FUNCTION update_comment_count();""",
        """CREATE TABLE IF NOT EXISTS leaderboards (
    id SERIAL PRIMARY KEY,
    submitter TEXT REFERENCES accounts(sonolus_id) ON DELETE CASCADE,
    replay_data_hash TEXT NOT NULL,
    replay_config_hash TEXT NOT NULL,
    chart_id TEXT REFERENCES charts(id) ON DELETE CASCADE,
    engine TEXT NOT NULL,
    grade TEXT NOT NULL,
    nperfect INTEGER NOT NULL,
    ngreat INTEGER NOT NULL,
    ngood INTEGER NOT NULL,
    nmiss INTEGER NOT NULL,
    arcade_score INTEGER NOT NULL,
    accuracy_score INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    public_chart BOOLEAN NOT NULL,
    speed DECIMAL,
    created_at timestamp with time zone DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
);""",
        """CREATE OR REPLACE FUNCTION update_like_count()
RETURNS TRIGGER AS $$
DECLARE
    a DOUBLE PRECISION := 1.0 / EXTRACT(EPOCH FROM INTERVAL '7 days');
    tnow DOUBLE PRECISION := EXTRACT(EPOCH FROM NOW());
    s DOUBLE PRECISION;
BEGIN
    IF EXISTS (SELECT 1 FROM charts WHERE id = COALESCE(NEW.chart_id, OLD.chart_id)) THEN

        IF TG_OP = 'INSERT' THEN
            -- increment like_count
            UPDATE charts
            SET like_count = like_count + 1
            WHERE id = NEW.chart_id;

            -- update log_like_score
            SELECT COALESCE(
                LN(
                    1 + EXP(a * (EXTRACT(EPOCH FROM NEW.created_at) - tnow)) /
                        EXP(COALESCE(c.log_like_score, 0))
                ),
                a * (EXTRACT(EPOCH FROM NEW.created_at) - tnow)
            )
            INTO s
            FROM charts c
            WHERE c.id = NEW.chart_id;

            UPDATE charts
            SET log_like_score = COALESCE(log_like_score, 0) + s
            WHERE id = NEW.chart_id;

        ELSIF TG_OP = 'DELETE' THEN
            -- decrement like_count
            UPDATE charts
            SET like_count = like_count - 1
            WHERE id = OLD.chart_id;

            -- recalc log_like_score from remaining likes
            UPDATE charts c
            SET log_like_score = COALESCE((
                SELECT LN(SUM(EXP(a * (EXTRACT(EPOCH FROM cl.created_at) - tnow))))
                FROM chart_likes cl
                WHERE cl.chart_id = OLD.chart_id
            ), 0)
            WHERE c.id = OLD.chart_id;

        END IF;

    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_update_like_count ON chart_likes;

CREATE TRIGGER trg_update_like_count
AFTER INSERT OR DELETE ON chart_likes
FOR EACH ROW
EXECUTE FUNCTION update_like_count();""",
        """-- Scalar columns: B-Tree
CREATE INDEX IF NOT EXISTS idx_charts_status ON charts(status);
CREATE INDEX IF NOT EXISTS idx_charts_rating ON charts(rating);
CREATE INDEX IF NOT EXISTS idx_charts_created_at ON charts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_charts_like_count ON charts(like_count DESC);
CREATE INDEX IF NOT EXISTS idx_chart_likes_user ON chart_likes(sonolus_id);
CREATE INDEX IF NOT EXISTS idx_chart_likes_chart ON chart_likes(chart_id);
CREATE INDEX IF NOT EXISTS idx_chart_likes_chart_created
    ON chart_likes (chart_id, created_at DESC);

-- GIN
CREATE INDEX IF NOT EXISTS idx_charts_tags ON charts USING GIN(tags);

-- Text columns with pg_trgm for fast ILIKE
CREATE INDEX IF NOT EXISTS idx_charts_title_trgm ON charts USING GIN (LOWER(title) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_charts_description_trgm ON charts USING GIN (LOWER(description) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_charts_artists_trgm ON charts USING GIN (LOWER(artists) gin_trgm_ops);
""",
        """CREATE TABLE IF NOT EXISTS external_login_ids (
    id_key TEXT NOT NULL PRIMARY KEY,
    session_key TEXT,
    expires_at timestamp with time zone DEFAULT ((CURRENT_TIMESTAMP + INTERVAL '6 minutes') AT TIME ZONE 'UTC')
);
CREATE INDEX IF NOT EXISTS idx_expires_at ON external_login_ids (expires_at);""",
        # """SELECT cron.schedule(
        #     'delete_expired_login_ids',
        #     '* * * * *', -- every minute
        #     'DELETE FROM external_login_ids WHERE expires_at < CURRENT_TIMESTAMP;'
        # );""",
        # """
        # SELECT cron.schedule(
        # 'publish_scheduled_charts',
        # '* * * * *', -- every minute
        # $$
        # UPDATE charts
        # SET
        #     status = 'PUBLIC'::chart_status,
        #     scheduled_publish = NULL,
        #     updated_at = CURRENT_TIMESTAMP,
        #     published_at = CASE
        #         WHEN published_at IS NULL THEN CURRENT_TIMESTAMP
        #         ELSE published_at
        #     END
        # WHERE
        #     scheduled_publish IS NOT NULL
        #     AND scheduled_publish <= CURRENT_TIMESTAMP
        #     AND status <> 'PUBLIC'::chart_status;
        # $$
        # );
        # """
        # superuser to schedule
    ]

    async with db.acquire() as connection:
        for query in queries:
            try:
                await connection.execute(query)
            except asyncpg.exceptions.InsufficientPrivilegeError as e:
                print(f"Permission denied: {e}")
            except asyncpg.exceptions.PostgresSyntaxError:
                print(query)
                raise
    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
