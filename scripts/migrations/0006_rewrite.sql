SET LOCAL lock_timeout = '10s';

DO $$
DECLARE
    filled TEXT;
    has_data BOOLEAN;
BEGIN
    SELECT string_agg(format('%I IS NOT NULL', column_name), ' OR ')
    INTO filled
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'accounts'
      AND column_name IN ('discord_id', 'patreon_id', 'oauth_details', 'subscription_details');
    IF filled IS NOT NULL THEN
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM accounts WHERE %s)', filled) INTO has_data;
        IF has_data THEN
            RAISE EXCEPTION 'accounts still has data in discord_id, patreon_id, oauth_details or subscription_details, which this migration drops; export or clear those columns first';
        END IF;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION like_weight(created_at timestamptz) RETURNS double precision
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT EXTRACT(EPOCH FROM (created_at - TIMESTAMPTZ '2025-01-01 00:00:00+00')) / 604800.0
$$;

CREATE OR REPLACE FUNCTION update_like_count() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
DECLARE
    weight DOUBLE PRECISION;
    previous_count BIGINT;
    previous_score DOUBLE PRECISION;
BEGIN
    IF TG_OP = 'INSERT' THEN
        weight := like_weight(NEW.created_at);
        SELECT like_count, log_like_score INTO previous_count, previous_score
        FROM charts WHERE id = NEW.chart_id FOR NO KEY UPDATE;
        IF NOT FOUND THEN
            RETURN NULL;
        END IF;
        UPDATE charts
        SET like_count = like_count + 1,
            log_like_score = CASE
                WHEN previous_count <= 0 THEN weight
                ELSE GREATEST(previous_score, weight) + LN(1 + EXP(-ABS(previous_score - weight)))
            END
        WHERE id = NEW.chart_id;
    ELSIF TG_OP = 'DELETE' THEN
        PERFORM 1 FROM charts WHERE id = OLD.chart_id FOR NO KEY UPDATE;
        UPDATE charts c
        SET like_count = GREATEST(c.like_count - 1, 0),
            log_like_score = COALESCE((
                SELECT t.top + LN(SUM(EXP(t.w - t.top)))
                FROM (
                    SELECT like_weight(cl.created_at) AS w,
                           MAX(like_weight(cl.created_at)) OVER () AS top
                    FROM chart_likes cl
                    WHERE cl.chart_id = OLD.chart_id
                ) t
                GROUP BY t.top
            ), 0)
        WHERE c.id = OLD.chart_id;
    END IF;
    RETURN NULL;
END;
$$;

UPDATE charts c
SET like_count = 0, log_like_score = 0
WHERE NOT EXISTS (SELECT 1 FROM chart_likes cl WHERE cl.chart_id = c.id);

UPDATE charts c
SET like_count = s.like_count, log_like_score = s.score
FROM (
    SELECT t.chart_id, COUNT(*) AS like_count, t.top + LN(SUM(EXP(t.w - t.top))) AS score
    FROM (
        SELECT chart_id,
               like_weight(created_at) AS w,
               MAX(like_weight(created_at)) OVER (PARTITION BY chart_id) AS top
        FROM chart_likes
    ) t
    GROUP BY t.chart_id, t.top
) s
WHERE c.id = s.chart_id;

DELETE FROM leaderboards l
USING (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY chart_id, submitter
               ORDER BY effective_score DESC, created_at DESC, id DESC
           ) AS rank
    FROM (
        SELECT id, chart_id, submitter, created_at,
               FLOOR(arcade_score * (
                   CASE
                       WHEN speed IS NULL THEN 1.0
                       WHEN FLOOR(speed * 10) / 10 < 1 THEN FLOOR(speed * 10) / 10 - 0.4
                       ELSE 1 + (FLOOR(speed * 10) / 10 - 1) * 0.075
                   END
               )) AS effective_score
        FROM leaderboards
        WHERE chart_id IS NOT NULL AND submitter IS NOT NULL
    ) scored
) ranked
WHERE l.id = ranked.id AND ranked.rank > 1;

CREATE UNIQUE INDEX IF NOT EXISTS leaderboards_chart_submitter_key ON leaderboards (chart_id, submitter);
CREATE INDEX IF NOT EXISTS idx_leaderboards_public_created_at ON leaderboards (created_at DESC) WHERE public_chart;

CREATE INDEX IF NOT EXISTS idx_charts_public_log_like_score ON charts (log_like_score DESC) WHERE status = 'PUBLIC' AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_charts_comment_count ON charts (comment_count DESC);
CREATE INDEX IF NOT EXISTS idx_charts_title ON charts (title);
CREATE INDEX IF NOT EXISTS idx_charts_scheduled_publish ON charts (scheduled_publish) WHERE scheduled_publish IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_comments_commenter ON comments (commenter);
CREATE INDEX IF NOT EXISTS idx_oauth_apps_owner ON oauth_apps (owner_id);
CREATE INDEX IF NOT EXISTS idx_oauth_codes_client ON oauth_authorization_codes (client_id);
CREATE INDEX IF NOT EXISTS idx_oauth_codes_user ON oauth_authorization_codes (user_id);
CREATE INDEX IF NOT EXISTS idx_oauth_tokens_client ON oauth_tokens (client_id);
CREATE INDEX IF NOT EXISTS idx_promotion_user_views_account ON promotion_user_views (sonolus_id);
CREATE INDEX IF NOT EXISTS idx_staff_actions_actor ON staff_actions (actor_id);

CREATE TABLE IF NOT EXISTS promotion_viewers (
    promotion_id INTEGER NOT NULL REFERENCES promotions(id) ON DELETE CASCADE,
    viewer_key TEXT NOT NULL,
    view_code TEXT NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (promotion_id, viewer_key)
);
CREATE INDEX IF NOT EXISTS idx_promotion_viewers_first_seen_at ON promotion_viewers (first_seen_at);

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sonolus_id TEXT NOT NULL REFERENCES accounts(sonolus_id) ON DELETE CASCADE,
    type TEXT NOT NULL CHECK (type IN ('game', 'website')),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_account_type ON sessions (sonolus_id, type, expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions (expires_at);

INSERT INTO sessions (sonolus_id, type, token_hash, expires_at)
SELECT a.sonolus_id,
       CASE slots.key WHEN 'game' THEN 'game' ELSE 'website' END,
       encode(sha256(convert_to(slot.value ->> 'session_key', 'UTF8')), 'hex'),
       to_timestamp(slot.expires_ms / 1000.0)
FROM accounts a,
     jsonb_each(
         CASE WHEN jsonb_typeof(a.sonolus_sessions) = 'object' THEN a.sonolus_sessions ELSE '{}'::jsonb END
     ) slots,
     LATERAL (
         SELECT s.value,
                CASE WHEN (s.value ->> 'expires') ~ '^[0-9]{1,15}$'
                     THEN (s.value ->> 'expires')::bigint
                END AS expires_ms
         FROM jsonb_each(
             CASE WHEN jsonb_typeof(slots.value) = 'object' THEN slots.value ELSE '{}'::jsonb END
         ) s
         WHERE jsonb_typeof(s.value) = 'object'
     ) slot
WHERE slots.key IN ('game', 'external')
  AND slot.value ->> 'session_key' IS NOT NULL
  AND slot.expires_ms > EXTRACT(EPOCH FROM CURRENT_TIMESTAMP) * 1000
ON CONFLICT (token_hash) DO NOTHING;

CREATE TABLE IF NOT EXISTS account_permissions (
    sonolus_id TEXT NOT NULL REFERENCES accounts(sonolus_id) ON DELETE CASCADE,
    permission TEXT NOT NULL CHECK (permission IN (
        'chart.visibility', 'chart.delete', 'chart.restore', 'chart.view_hidden', 'chart.rerate',
        'chart.staff_pick', 'comment.delete', 'leaderboard.delete', 'notification.send',
        'account.ban', 'account.delete', 'staff.manage'
    )),
    granted_by TEXT REFERENCES accounts(sonolus_id) ON DELETE SET NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sonolus_id, permission)
);
CREATE INDEX IF NOT EXISTS idx_account_permissions_granted_by ON account_permissions (granted_by);

INSERT INTO account_permissions (sonolus_id, permission)
SELECT a.sonolus_id, p.permission
FROM accounts a
CROSS JOIN unnest(ARRAY[
    'chart.visibility', 'chart.delete', 'chart.restore', 'chart.view_hidden', 'chart.rerate',
    'chart.staff_pick', 'comment.delete', 'leaderboard.delete', 'notification.send'
]) AS p(permission)
WHERE a.mod OR a.admin
ON CONFLICT DO NOTHING;

INSERT INTO account_permissions (sonolus_id, permission)
SELECT a.sonolus_id, p.permission
FROM accounts a
CROSS JOIN unnest(ARRAY['account.ban', 'account.delete', 'staff.manage']) AS p(permission)
WHERE a.admin
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS account_connections (
    sonolus_id TEXT NOT NULL REFERENCES accounts(sonolus_id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('discord', 'google')),
    provider_user_id TEXT NOT NULL,
    provider_display_name TEXT,
    access_token_encrypted BYTEA,
    refresh_token_encrypted BYTEA,
    token_expires_at TIMESTAMPTZ,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sonolus_id, provider),
    UNIQUE (provider, provider_user_id)
);

CREATE TABLE IF NOT EXISTS chart_collaborators (
    chart_id TEXT NOT NULL REFERENCES charts(id) ON DELETE CASCADE,
    sonolus_id TEXT NOT NULL REFERENCES accounts(sonolus_id) ON DELETE CASCADE,
    permissions TEXT[] NOT NULL DEFAULT '{}' CHECK (
        permissions <@ ARRAY['read', 'edit.files', 'edit.metadata', 'visibility', 'credit', 'manage']::text[]
    ),
    added_by TEXT REFERENCES accounts(sonolus_id) ON DELETE SET NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (chart_id, sonolus_id)
);
CREATE INDEX IF NOT EXISTS idx_chart_collaborators_account ON chart_collaborators (sonolus_id);
CREATE INDEX IF NOT EXISTS idx_chart_collaborators_added_by ON chart_collaborators (added_by);

CREATE TABLE IF NOT EXISTS chart_collaborator_invites (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chart_id TEXT NOT NULL REFERENCES charts(id) ON DELETE CASCADE,
    sonolus_id TEXT NOT NULL REFERENCES accounts(sonolus_id) ON DELETE CASCADE,
    permissions TEXT[] NOT NULL DEFAULT '{}' CHECK (
        permissions <@ ARRAY['read', 'edit.files', 'edit.metadata', 'visibility', 'credit', 'manage']::text[]
    ),
    invited_by TEXT REFERENCES accounts(sonolus_id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (chart_id, sonolus_id)
);
CREATE INDEX IF NOT EXISTS idx_chart_collaborator_invites_account ON chart_collaborator_invites (sonolus_id);
CREATE INDEX IF NOT EXISTS idx_chart_collaborator_invites_invited_by ON chart_collaborator_invites (invited_by);

ALTER TABLE staff_actions ADD COLUMN IF NOT EXISTS via_admin_key BOOLEAN NOT NULL DEFAULT false;

ALTER TABLE leaderboards ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE oauth_apps ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE oauth_apps ALTER COLUMN updated_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE oauth_authorization_codes ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE oauth_tokens ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE promotion_views ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE promotions ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE staff_actions ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;

ALTER TABLE external_login_ids ALTER COLUMN expires_at SET DEFAULT (CURRENT_TIMESTAMP + INTERVAL '5 minutes');

ALTER TABLE accounts
    DROP COLUMN IF EXISTS sonolus_sessions,
    DROP COLUMN IF EXISTS mod,
    DROP COLUMN IF EXISTS admin,
    DROP COLUMN IF EXISTS discord_id,
    DROP COLUMN IF EXISTS patreon_id,
    DROP COLUMN IF EXISTS oauth_details,
    DROP COLUMN IF EXISTS subscription_details;

CREATE TABLE IF NOT EXISTS account_settings (
    sonolus_id TEXT PRIMARY KEY REFERENCES accounts(sonolus_id) ON DELETE CASCADE,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(settings) = 'object'),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS account_badges (
    sonolus_id TEXT NOT NULL REFERENCES accounts(sonolus_id) ON DELETE CASCADE,
    badge TEXT NOT NULL CHECK (badge IN ('owner', 'developer')),
    level SMALLINT NOT NULL DEFAULT 1 CHECK (level >= 1),
    granted_by TEXT REFERENCES accounts(sonolus_id) ON DELETE SET NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (sonolus_id, badge)
);
CREATE INDEX IF NOT EXISTS idx_account_badges_granted_by ON account_badges (granted_by);

ALTER TABLE charts ADD COLUMN IF NOT EXISTS credit_search TEXT NOT NULL DEFAULT '';
ALTER TABLE charts ADD COLUMN IF NOT EXISTS chart_seconds DOUBLE PRECISION;

CREATE OR REPLACE FUNCTION chart_credit_search(p_chart_id text, p_chart_author text, p_owner_id text) RETURNS text
    LANGUAGE plpgsql STABLE
    AS $$
BEGIN
    RETURN lower(concat_ws(
        E'\n',
        p_chart_author || '#' || (SELECT a.sonolus_handle FROM accounts a WHERE a.sonolus_id = p_owner_id),
        (
            SELECT string_agg(a.sonolus_username || '#' || a.sonolus_handle, E'\n' ORDER BY cc.added_at, cc.sonolus_id)
            FROM chart_collaborators cc
            JOIN accounts a ON a.sonolus_id = cc.sonolus_id
            WHERE cc.chart_id = p_chart_id AND 'credit' = ANY (cc.permissions)
        )
    ));
END;
$$;

CREATE OR REPLACE FUNCTION set_chart_credit_search() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.credit_search := chart_credit_search(NEW.id, NEW.chart_author, NEW.author);
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION refresh_collaborator_credit_search() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        UPDATE charts c SET credit_search = chart_credit_search(c.id, c.chart_author, c.author)
        WHERE c.id = OLD.chart_id;
    END IF;
    IF TG_OP = 'INSERT' OR (TG_OP = 'UPDATE' AND NEW.chart_id IS DISTINCT FROM OLD.chart_id) THEN
        UPDATE charts c SET credit_search = chart_credit_search(c.id, c.chart_author, c.author)
        WHERE c.id = NEW.chart_id;
    END IF;
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION refresh_account_credit_search() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE charts c SET credit_search = chart_credit_search(c.id, c.chart_author, c.author)
    WHERE c.author = NEW.sonolus_id
       OR c.id IN (SELECT cc.chart_id FROM chart_collaborators cc WHERE cc.sonolus_id = NEW.sonolus_id);
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_chart_credit_search ON charts;
CREATE TRIGGER trg_chart_credit_search BEFORE INSERT OR UPDATE OF chart_author, author ON charts
    FOR EACH ROW EXECUTE FUNCTION set_chart_credit_search();

DROP TRIGGER IF EXISTS trg_collaborator_credit_search ON chart_collaborators;
CREATE TRIGGER trg_collaborator_credit_search AFTER INSERT OR DELETE OR UPDATE OF chart_id, permissions ON chart_collaborators
    FOR EACH ROW EXECUTE FUNCTION refresh_collaborator_credit_search();

DROP TRIGGER IF EXISTS trg_account_credit_search ON accounts;
CREATE TRIGGER trg_account_credit_search AFTER UPDATE OF sonolus_username, sonolus_handle ON accounts
    FOR EACH ROW
    WHEN (OLD.sonolus_username IS DISTINCT FROM NEW.sonolus_username OR OLD.sonolus_handle IS DISTINCT FROM NEW.sonolus_handle)
    EXECUTE FUNCTION refresh_account_credit_search();

UPDATE charts SET credit_search = chart_credit_search(id, chart_author, author);

CREATE INDEX IF NOT EXISTS idx_charts_credit_search_trgm ON charts USING gin (credit_search gin_trgm_ops);
