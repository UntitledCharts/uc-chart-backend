CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

CREATE TYPE chart_status AS ENUM (
    'UNLISTED',
    'PRIVATE',
    'PUBLIC'
);

CREATE FUNCTION chart_credit_search(p_chart_id text, p_chart_author text, p_owner_id text) RETURNS text
    LANGUAGE plpgsql VOLATILE
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

CREATE FUNCTION decayed_like_score(created_at timestamp without time zone, halflife interval) RETURNS double precision
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT exp(-EXTRACT(EPOCH FROM (NOW() - created_at)) / EXTRACT(EPOCH FROM halflife));
$$;

CREATE FUNCTION like_weight(created_at timestamp with time zone) RETURNS double precision
    LANGUAGE sql IMMUTABLE
    AS $$
    SELECT EXTRACT(EPOCH FROM (created_at - TIMESTAMPTZ '2025-01-01 00:00:00+00')) / 604800.0
$$;

CREATE FUNCTION refresh_account_credit_search() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    UPDATE charts c SET credit_search = chart_credit_search(c.id, c.chart_author, c.author)
    WHERE c.author = NEW.sonolus_id
       OR c.id IN (SELECT cc.chart_id FROM chart_collaborators cc WHERE cc.sonolus_id = NEW.sonolus_id);
    RETURN NULL;
END;
$$;

CREATE FUNCTION refresh_collaborator_credit_search() RETURNS trigger
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

CREATE FUNCTION set_chart_credit_search() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    NEW.credit_search := chart_credit_search(NEW.id, NEW.chart_author, NEW.author);
    RETURN NEW;
END;
$$;

CREATE FUNCTION update_comment_count() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM charts WHERE id = COALESCE(NEW.chart_id, OLD.chart_id)) THEN

        IF TG_OP = 'INSERT' THEN
            UPDATE charts
            SET comment_count = comment_count + 1
            WHERE id = NEW.chart_id;

        ELSIF TG_OP = 'DELETE' THEN
            UPDATE charts
            SET comment_count = comment_count - 1
            WHERE id = OLD.chart_id;

        END IF;

    END IF;

    RETURN NULL;
END;
$$;

CREATE FUNCTION update_like_count() RETURNS trigger
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

CREATE TABLE account_badges (
    sonolus_id text NOT NULL,
    badge text NOT NULL,
    level smallint DEFAULT 1 NOT NULL,
    granted_by text,
    granted_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT account_badges_badge_check CHECK ((badge = ANY (ARRAY['owner'::text, 'developer'::text]))),
    CONSTRAINT account_badges_level_check CHECK ((level >= 1))
);

CREATE TABLE account_connections (
    sonolus_id text NOT NULL,
    provider text NOT NULL,
    provider_user_id text NOT NULL,
    provider_display_name text,
    access_token_encrypted bytea,
    refresh_token_encrypted bytea,
    token_expires_at timestamp with time zone,
    linked_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT account_connections_provider_check CHECK ((provider = ANY (ARRAY['discord'::text, 'google'::text])))
);

CREATE TABLE account_permissions (
    sonolus_id text NOT NULL,
    permission text NOT NULL,
    granted_by text,
    granted_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT account_permissions_permission_check CHECK ((permission = ANY (ARRAY['chart.visibility'::text, 'chart.delete'::text, 'chart.restore'::text, 'chart.view_hidden'::text, 'chart.rerate'::text, 'chart.staff_pick'::text, 'comment.delete'::text, 'leaderboard.delete'::text, 'notification.send'::text, 'account.ban'::text, 'account.delete'::text, 'staff.manage'::text])))
);

CREATE TABLE account_settings (
    sonolus_id text NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT account_settings_settings_check CHECK ((jsonb_typeof(settings) = 'object'::text))
);

CREATE TABLE accounts (
    sonolus_id text NOT NULL,
    sonolus_handle bigint NOT NULL,
    chart_upload_cooldown timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    banned boolean DEFAULT false,
    sonolus_username text NOT NULL,
    description text,
    profile_hash text,
    banner_hash text,
    deleted_at timestamp with time zone,
    ad_views bigint DEFAULT 0 NOT NULL,
    ad_clicks bigint DEFAULT 0 NOT NULL,
    recent_opened_levels integer[] DEFAULT '{}'::integer[] NOT NULL
);

CREATE TABLE chart_collaborator_invites (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    chart_id text NOT NULL,
    sonolus_id text NOT NULL,
    permissions text[] DEFAULT '{}'::text[] NOT NULL,
    invited_by text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT chart_collaborator_invites_permissions_check CHECK ((permissions <@ ARRAY['read'::text, 'edit.files'::text, 'edit.metadata'::text, 'visibility'::text, 'credit'::text, 'manage'::text]))
);

CREATE TABLE chart_collaborators (
    chart_id text NOT NULL,
    sonolus_id text NOT NULL,
    permissions text[] DEFAULT '{}'::text[] NOT NULL,
    added_by text,
    added_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT chart_collaborators_permissions_check CHECK ((permissions <@ ARRAY['read'::text, 'edit.files'::text, 'edit.metadata'::text, 'visibility'::text, 'credit'::text, 'manage'::text]))
);

CREATE TABLE chart_likes (
    chart_id text NOT NULL,
    sonolus_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

CREATE TABLE charts (
    id text NOT NULL,
    rating numeric DEFAULT 1,
    author text,
    chart_author text NOT NULL,
    description text,
    title text NOT NULL,
    artists text,
    tags text[] DEFAULT '{}'::text[],
    like_count bigint DEFAULT 0 NOT NULL,
    log_like_score double precision DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    status chart_status NOT NULL,
    jacket_file_hash text NOT NULL,
    music_file_hash text NOT NULL,
    chart_file_hash text NOT NULL,
    preview_file_hash text,
    background_file_hash text,
    background_v1_file_hash text NOT NULL,
    background_v3_file_hash text NOT NULL,
    staff_pick boolean DEFAULT false,
    sponsored_until timestamp without time zone,
    comment_count bigint DEFAULT 0 NOT NULL,
    published_at timestamp with time zone,
    scheduled_publish timestamp with time zone,
    deleted_at timestamp with time zone,
    credit_search text DEFAULT ''::text NOT NULL,
    chart_seconds double precision
);

CREATE TABLE comments (
    id integer NOT NULL,
    commenter text,
    content text NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    deleted_at timestamp with time zone,
    chart_id text
);

CREATE SEQUENCE comments_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE comments_id_seq OWNED BY comments.id;

CREATE TABLE database_info (
    version integer NOT NULL
);

CREATE TABLE external_login_ids (
    id_key text NOT NULL,
    session_key text,
    expires_at timestamp with time zone DEFAULT (CURRENT_TIMESTAMP + '00:05:00'::interval)
);

CREATE TABLE leaderboards (
    id integer NOT NULL,
    submitter text,
    replay_data_hash text NOT NULL,
    replay_config_hash text NOT NULL,
    chart_id text,
    engine text NOT NULL,
    grade text NOT NULL,
    nperfect integer NOT NULL,
    ngreat integer NOT NULL,
    ngood integer NOT NULL,
    nmiss integer NOT NULL,
    arcade_score integer NOT NULL,
    accuracy_score integer NOT NULL,
    display_name text NOT NULL,
    public_chart boolean NOT NULL,
    speed numeric,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE leaderboards_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE leaderboards_id_seq OWNED BY leaderboards.id;

CREATE TABLE notifications (
    id integer NOT NULL,
    user_id text,
    title text NOT NULL,
    content text,
    is_read boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE notifications_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE notifications_id_seq OWNED BY notifications.id;

CREATE TABLE oauth_apps (
    client_id text NOT NULL,
    client_secret_hash text,
    name text NOT NULL,
    description text,
    owner_id text,
    redirect_uris text[] DEFAULT '{}'::text[] NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    public boolean DEFAULT false NOT NULL,
    CONSTRAINT confidential_apps_have_a_secret CHECK ((public OR (client_secret_hash IS NOT NULL)))
);

CREATE TABLE oauth_authorization_codes (
    code_hash text NOT NULL,
    client_id text NOT NULL,
    user_id text NOT NULL,
    scopes text[] DEFAULT '{}'::text[] NOT NULL,
    redirect_uri text NOT NULL,
    code_challenge text,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE oauth_tokens (
    id integer NOT NULL,
    access_token_hash text NOT NULL,
    refresh_token_hash text NOT NULL,
    client_id text NOT NULL,
    user_id text NOT NULL,
    scopes text[] DEFAULT '{}'::text[] NOT NULL,
    access_expires_at timestamp with time zone NOT NULL,
    refresh_expires_at timestamp with time zone NOT NULL,
    revoked boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE SEQUENCE oauth_tokens_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE oauth_tokens_id_seq OWNED BY oauth_tokens.id;

CREATE TABLE promotion_user_views (
    promotion_id integer NOT NULL,
    sonolus_id text NOT NULL,
    shown_count integer DEFAULT 0 NOT NULL
);

CREATE TABLE promotion_viewers (
    promotion_id integer NOT NULL,
    viewer_key text NOT NULL,
    view_code text NOT NULL,
    first_seen_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);

CREATE TABLE promotion_views (
    promotion_id integer NOT NULL,
    view_code text NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE promotions (
    id integer NOT NULL,
    chart_id text NOT NULL,
    target_type text NOT NULL,
    target_amount integer NOT NULL,
    view_count bigint DEFAULT 0 NOT NULL,
    click_count bigint DEFAULT 0 NOT NULL,
    logged_in_click_count bigint DEFAULT 0 NOT NULL,
    cancelled boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    ended_at timestamp with time zone,
    CONSTRAINT promotions_target_type_check CHECK ((target_type = ANY (ARRAY['VIEW'::text, 'CLICK'::text])))
);

CREATE SEQUENCE promotions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE promotions_id_seq OWNED BY promotions.id;

CREATE TABLE sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    sonolus_id text NOT NULL,
    type text NOT NULL,
    token_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT sessions_type_check CHECK ((type = ANY (ARRAY['game'::text, 'website'::text])))
);

CREATE TABLE staff_actions (
    id integer NOT NULL,
    actor_id text,
    action text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    previous_value text,
    new_value text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP,
    via_admin_key boolean DEFAULT false NOT NULL
);

CREATE SEQUENCE staff_actions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

ALTER SEQUENCE staff_actions_id_seq OWNED BY staff_actions.id;

ALTER TABLE ONLY comments ALTER COLUMN id SET DEFAULT nextval('comments_id_seq'::regclass);

ALTER TABLE ONLY leaderboards ALTER COLUMN id SET DEFAULT nextval('leaderboards_id_seq'::regclass);

ALTER TABLE ONLY notifications ALTER COLUMN id SET DEFAULT nextval('notifications_id_seq'::regclass);

ALTER TABLE ONLY oauth_tokens ALTER COLUMN id SET DEFAULT nextval('oauth_tokens_id_seq'::regclass);

ALTER TABLE ONLY promotions ALTER COLUMN id SET DEFAULT nextval('promotions_id_seq'::regclass);

ALTER TABLE ONLY staff_actions ALTER COLUMN id SET DEFAULT nextval('staff_actions_id_seq'::regclass);

ALTER TABLE ONLY account_badges
    ADD CONSTRAINT account_badges_pkey PRIMARY KEY (sonolus_id, badge);

ALTER TABLE ONLY account_connections
    ADD CONSTRAINT account_connections_pkey PRIMARY KEY (sonolus_id, provider);

ALTER TABLE ONLY account_connections
    ADD CONSTRAINT account_connections_provider_provider_user_id_key UNIQUE (provider, provider_user_id);

ALTER TABLE ONLY account_permissions
    ADD CONSTRAINT account_permissions_pkey PRIMARY KEY (sonolus_id, permission);

ALTER TABLE ONLY account_settings
    ADD CONSTRAINT account_settings_pkey PRIMARY KEY (sonolus_id);

ALTER TABLE ONLY accounts
    ADD CONSTRAINT accounts_pkey PRIMARY KEY (sonolus_id);

ALTER TABLE ONLY chart_collaborator_invites
    ADD CONSTRAINT chart_collaborator_invites_chart_id_sonolus_id_key UNIQUE (chart_id, sonolus_id);

ALTER TABLE ONLY chart_collaborator_invites
    ADD CONSTRAINT chart_collaborator_invites_pkey PRIMARY KEY (id);

ALTER TABLE ONLY chart_collaborators
    ADD CONSTRAINT chart_collaborators_pkey PRIMARY KEY (chart_id, sonolus_id);

ALTER TABLE ONLY chart_likes
    ADD CONSTRAINT chart_likes_pkey PRIMARY KEY (chart_id, sonolus_id);

ALTER TABLE ONLY charts
    ADD CONSTRAINT charts_pkey PRIMARY KEY (id);

ALTER TABLE ONLY comments
    ADD CONSTRAINT comments_pkey PRIMARY KEY (id);

ALTER TABLE ONLY database_info
    ADD CONSTRAINT database_info_pkey PRIMARY KEY (version);

ALTER TABLE ONLY external_login_ids
    ADD CONSTRAINT external_login_ids_pkey PRIMARY KEY (id_key);

ALTER TABLE ONLY leaderboards
    ADD CONSTRAINT leaderboards_pkey PRIMARY KEY (id);

ALTER TABLE ONLY notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);

ALTER TABLE ONLY oauth_apps
    ADD CONSTRAINT oauth_apps_pkey PRIMARY KEY (client_id);

ALTER TABLE ONLY oauth_authorization_codes
    ADD CONSTRAINT oauth_authorization_codes_pkey PRIMARY KEY (code_hash);

ALTER TABLE ONLY oauth_tokens
    ADD CONSTRAINT oauth_tokens_access_token_hash_key UNIQUE (access_token_hash);

ALTER TABLE ONLY oauth_tokens
    ADD CONSTRAINT oauth_tokens_pkey PRIMARY KEY (id);

ALTER TABLE ONLY oauth_tokens
    ADD CONSTRAINT oauth_tokens_refresh_token_hash_key UNIQUE (refresh_token_hash);

ALTER TABLE ONLY promotion_user_views
    ADD CONSTRAINT promotion_user_views_pkey PRIMARY KEY (promotion_id, sonolus_id);

ALTER TABLE ONLY promotion_viewers
    ADD CONSTRAINT promotion_viewers_pkey PRIMARY KEY (promotion_id, viewer_key);

ALTER TABLE ONLY promotion_views
    ADD CONSTRAINT promotion_views_pkey PRIMARY KEY (promotion_id, view_code);

ALTER TABLE ONLY promotions
    ADD CONSTRAINT promotions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);

ALTER TABLE ONLY sessions
    ADD CONSTRAINT sessions_token_hash_key UNIQUE (token_hash);

ALTER TABLE ONLY staff_actions
    ADD CONSTRAINT staff_actions_pkey PRIMARY KEY (id);

CREATE INDEX idx_account_badges_granted_by ON account_badges USING btree (granted_by);

CREATE INDEX idx_account_permissions_granted_by ON account_permissions USING btree (granted_by);

CREATE INDEX idx_accounts_deleted_at ON accounts USING btree (deleted_at) WHERE (deleted_at IS NOT NULL);

CREATE INDEX idx_accounts_handle ON accounts USING btree (sonolus_handle);

CREATE INDEX idx_chart_collaborator_invites_account ON chart_collaborator_invites USING btree (sonolus_id);

CREATE INDEX idx_chart_collaborator_invites_invited_by ON chart_collaborator_invites USING btree (invited_by);

CREATE INDEX idx_chart_collaborators_account ON chart_collaborators USING btree (sonolus_id);

CREATE INDEX idx_chart_collaborators_added_by ON chart_collaborators USING btree (added_by);

CREATE INDEX idx_chart_likes_chart ON chart_likes USING btree (chart_id);

CREATE INDEX idx_chart_likes_chart_created ON chart_likes USING btree (chart_id, created_at DESC);

CREATE INDEX idx_chart_likes_user ON chart_likes USING btree (sonolus_id);

CREATE INDEX idx_charts_artists_trgm ON charts USING gin (lower(artists) gin_trgm_ops);

CREATE INDEX idx_charts_author ON charts USING btree (author);

CREATE INDEX idx_charts_comment_count ON charts USING btree (comment_count DESC);

CREATE INDEX idx_charts_created_at ON charts USING btree (created_at DESC);

CREATE INDEX idx_charts_credit_search_trgm ON charts USING gin (credit_search gin_trgm_ops);

CREATE INDEX idx_charts_deleted_at ON charts USING btree (deleted_at) WHERE (deleted_at IS NOT NULL);

CREATE INDEX idx_charts_description_trgm ON charts USING gin (lower(description) gin_trgm_ops);

CREATE INDEX idx_charts_like_count ON charts USING btree (like_count DESC);

CREATE INDEX idx_charts_public_log_like_score ON charts USING btree (log_like_score DESC) WHERE ((status = 'PUBLIC'::chart_status) AND (deleted_at IS NULL));

CREATE INDEX idx_charts_published_at ON charts USING btree (published_at DESC);

CREATE INDEX idx_charts_rating ON charts USING btree (rating);

CREATE INDEX idx_charts_scheduled_publish ON charts USING btree (scheduled_publish) WHERE (scheduled_publish IS NOT NULL);

CREATE INDEX idx_charts_status ON charts USING btree (status);

CREATE INDEX idx_charts_tags ON charts USING gin (tags);

CREATE INDEX idx_charts_title ON charts USING btree (title);

CREATE INDEX idx_charts_title_trgm ON charts USING gin (lower(title) gin_trgm_ops);

CREATE INDEX idx_comments_chart_created ON comments USING btree (chart_id, created_at DESC);

CREATE INDEX idx_comments_chart_id ON comments USING btree (chart_id);

CREATE INDEX idx_comments_commenter ON comments USING btree (commenter);

CREATE INDEX idx_expires_at ON external_login_ids USING btree (expires_at);

CREATE INDEX idx_leaderboards_chart_id ON leaderboards USING btree (chart_id);

CREATE INDEX idx_leaderboards_chart_score ON leaderboards USING btree (chart_id, arcade_score DESC);

CREATE INDEX idx_leaderboards_public_created_at ON leaderboards USING btree (created_at DESC) WHERE public_chart;

CREATE INDEX idx_leaderboards_submitter ON leaderboards USING btree (submitter);

CREATE INDEX idx_notifications_user_id ON notifications USING btree (user_id, created_at DESC);

CREATE INDEX idx_oauth_apps_owner ON oauth_apps USING btree (owner_id);

CREATE INDEX idx_oauth_codes_client ON oauth_authorization_codes USING btree (client_id);

CREATE INDEX idx_oauth_codes_expires_at ON oauth_authorization_codes USING btree (expires_at);

CREATE INDEX idx_oauth_codes_user ON oauth_authorization_codes USING btree (user_id);

CREATE INDEX idx_oauth_tokens_client ON oauth_tokens USING btree (client_id);

CREATE INDEX idx_oauth_tokens_refresh_expires_at ON oauth_tokens USING btree (refresh_expires_at);

CREATE INDEX idx_oauth_tokens_user ON oauth_tokens USING btree (user_id);

CREATE INDEX idx_promotion_user_views_account ON promotion_user_views USING btree (sonolus_id);

CREATE INDEX idx_promotion_viewers_first_seen_at ON promotion_viewers USING btree (first_seen_at);

CREATE INDEX idx_promotion_views_created_at ON promotion_views USING btree (created_at);

CREATE INDEX idx_promotions_active ON promotions USING btree (chart_id) WHERE (cancelled = false);

CREATE INDEX idx_sessions_account_type ON sessions USING btree (sonolus_id, type, expires_at);

CREATE INDEX idx_sessions_expires_at ON sessions USING btree (expires_at);

CREATE INDEX idx_staff_actions_actor ON staff_actions USING btree (actor_id);

CREATE INDEX idx_staff_actions_created_at ON staff_actions USING btree (created_at DESC);

CREATE UNIQUE INDEX leaderboards_chart_submitter_key ON leaderboards USING btree (chart_id, submitter);

CREATE TRIGGER trg_account_credit_search AFTER UPDATE OF sonolus_username, sonolus_handle ON accounts FOR EACH ROW WHEN (((old.sonolus_username IS DISTINCT FROM new.sonolus_username) OR (old.sonolus_handle IS DISTINCT FROM new.sonolus_handle))) EXECUTE FUNCTION refresh_account_credit_search();

CREATE TRIGGER trg_chart_credit_search BEFORE INSERT OR UPDATE OF chart_author, author ON charts FOR EACH ROW EXECUTE FUNCTION set_chart_credit_search();

CREATE TRIGGER trg_collaborator_credit_search AFTER INSERT OR DELETE OR UPDATE OF chart_id, permissions ON chart_collaborators FOR EACH ROW EXECUTE FUNCTION refresh_collaborator_credit_search();

CREATE TRIGGER trg_update_comment_count AFTER INSERT OR DELETE ON comments FOR EACH ROW EXECUTE FUNCTION update_comment_count();

CREATE TRIGGER trg_update_like_count AFTER INSERT OR DELETE ON chart_likes FOR EACH ROW EXECUTE FUNCTION update_like_count();

ALTER TABLE ONLY account_badges
    ADD CONSTRAINT account_badges_granted_by_fkey FOREIGN KEY (granted_by) REFERENCES accounts(sonolus_id) ON DELETE SET NULL;

ALTER TABLE ONLY account_badges
    ADD CONSTRAINT account_badges_sonolus_id_fkey FOREIGN KEY (sonolus_id) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY account_connections
    ADD CONSTRAINT account_connections_sonolus_id_fkey FOREIGN KEY (sonolus_id) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY account_permissions
    ADD CONSTRAINT account_permissions_granted_by_fkey FOREIGN KEY (granted_by) REFERENCES accounts(sonolus_id) ON DELETE SET NULL;

ALTER TABLE ONLY account_permissions
    ADD CONSTRAINT account_permissions_sonolus_id_fkey FOREIGN KEY (sonolus_id) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY account_settings
    ADD CONSTRAINT account_settings_sonolus_id_fkey FOREIGN KEY (sonolus_id) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY chart_collaborator_invites
    ADD CONSTRAINT chart_collaborator_invites_chart_id_fkey FOREIGN KEY (chart_id) REFERENCES charts(id) ON DELETE CASCADE;

ALTER TABLE ONLY chart_collaborator_invites
    ADD CONSTRAINT chart_collaborator_invites_invited_by_fkey FOREIGN KEY (invited_by) REFERENCES accounts(sonolus_id) ON DELETE SET NULL;

ALTER TABLE ONLY chart_collaborator_invites
    ADD CONSTRAINT chart_collaborator_invites_sonolus_id_fkey FOREIGN KEY (sonolus_id) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY chart_collaborators
    ADD CONSTRAINT chart_collaborators_added_by_fkey FOREIGN KEY (added_by) REFERENCES accounts(sonolus_id) ON DELETE SET NULL;

ALTER TABLE ONLY chart_collaborators
    ADD CONSTRAINT chart_collaborators_chart_id_fkey FOREIGN KEY (chart_id) REFERENCES charts(id) ON DELETE CASCADE;

ALTER TABLE ONLY chart_collaborators
    ADD CONSTRAINT chart_collaborators_sonolus_id_fkey FOREIGN KEY (sonolus_id) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY chart_likes
    ADD CONSTRAINT chart_likes_chart_id_fkey FOREIGN KEY (chart_id) REFERENCES charts(id) ON DELETE CASCADE;

ALTER TABLE ONLY chart_likes
    ADD CONSTRAINT chart_likes_sonolus_id_fkey FOREIGN KEY (sonolus_id) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY charts
    ADD CONSTRAINT charts_author_fkey FOREIGN KEY (author) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY comments
    ADD CONSTRAINT comments_chart_id_fkey FOREIGN KEY (chart_id) REFERENCES charts(id) ON DELETE CASCADE;

ALTER TABLE ONLY comments
    ADD CONSTRAINT comments_commenter_fkey FOREIGN KEY (commenter) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY leaderboards
    ADD CONSTRAINT leaderboards_chart_id_fkey FOREIGN KEY (chart_id) REFERENCES charts(id) ON DELETE CASCADE;

ALTER TABLE ONLY leaderboards
    ADD CONSTRAINT leaderboards_submitter_fkey FOREIGN KEY (submitter) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY notifications
    ADD CONSTRAINT notifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY oauth_apps
    ADD CONSTRAINT oauth_apps_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES accounts(sonolus_id) ON DELETE SET NULL;

ALTER TABLE ONLY oauth_authorization_codes
    ADD CONSTRAINT oauth_authorization_codes_client_id_fkey FOREIGN KEY (client_id) REFERENCES oauth_apps(client_id) ON DELETE CASCADE;

ALTER TABLE ONLY oauth_authorization_codes
    ADD CONSTRAINT oauth_authorization_codes_user_id_fkey FOREIGN KEY (user_id) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY oauth_tokens
    ADD CONSTRAINT oauth_tokens_client_id_fkey FOREIGN KEY (client_id) REFERENCES oauth_apps(client_id) ON DELETE CASCADE;

ALTER TABLE ONLY oauth_tokens
    ADD CONSTRAINT oauth_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY promotion_user_views
    ADD CONSTRAINT promotion_user_views_promotion_id_fkey FOREIGN KEY (promotion_id) REFERENCES promotions(id) ON DELETE CASCADE;

ALTER TABLE ONLY promotion_user_views
    ADD CONSTRAINT promotion_user_views_sonolus_id_fkey FOREIGN KEY (sonolus_id) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY promotion_viewers
    ADD CONSTRAINT promotion_viewers_promotion_id_fkey FOREIGN KEY (promotion_id) REFERENCES promotions(id) ON DELETE CASCADE;

ALTER TABLE ONLY promotion_views
    ADD CONSTRAINT promotion_views_promotion_id_fkey FOREIGN KEY (promotion_id) REFERENCES promotions(id) ON DELETE CASCADE;

ALTER TABLE ONLY sessions
    ADD CONSTRAINT sessions_sonolus_id_fkey FOREIGN KEY (sonolus_id) REFERENCES accounts(sonolus_id) ON DELETE CASCADE;

ALTER TABLE ONLY staff_actions
    ADD CONSTRAINT staff_actions_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES accounts(sonolus_id) ON DELETE SET NULL;
