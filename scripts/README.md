# Scripts

Operational scripts. Run them from the repository root as `python -m scripts.<name>`. Every script takes `--config` (default `config.yml`, the same file the API uses); the ones marked *constants* also take `--constants` (default `constants.yml`). They work on the database and storage directly, not through the HTTP API.

Staff actions done by a script (bans, account deletion, permission changes) are logged in `staff_actions` exactly like an `X-Admin-Key` API call: `via_admin_key = true`, `actor_id = NULL`, with the API's action names. They are also announced on Discord when the bot and its `staff_actions` channel are configured. Scripts that need constants build the same `AppContext` an API worker uses, so `keys.encryption_key` must be valid.

Destructive scripts ask for confirmation before changing anything.

## Database setup and migrations

| Script | What it does | Flags |
| --- | --- | --- |
| `database_setup` | Creates a fresh database from `schema.sql` and stores the newest migration version in `database_info`. Refuses if the database already has any public table. pg_cron isn't needed; the API's worker publishes scheduled charts and deletes expired rows. | |
| `apply_database` | Migration runner. Reads `database_info.version` and lists the pending `migrations/NNNN_<slug>.sql` files (`NNNN` = zero-padded version the file migrates to) with their full SQL. With `--apply`, runs each in its own transaction that also bumps `database_info.version`, stopping at the first failure. Refuses databases without `database_info` (create them with `database_setup`) or older than version 5 (upgrade those with the old backend's `scripts/database_migration`). | `--apply` |
| `db_dump` | Dumps the database to plain SQL with `pg_dump` (`--no-owner --no-acl`, without the old `cron` schema). The default output folder `scripts/dumps/` gets a `.gitignore`. | `[output_file]`, `--pg-bin DIR` |
| `db_restore` | Drops and recreates the `public` schema, then restores a plain SQL dump with `psql` or a custom-format archive with `pg_restore`, both stopping at the first error inside a single transaction (`-v ON_ERROR_STOP=1 --single-transaction`, `--single-transaction --exit-on-error`). A failed plain SQL restore is rolled back together with the schema drop; a failed archive restore leaves the database empty. Asks you to type `yes`. | `input_file`, `--pg-bin DIR` |

Adding a migration: write `migrations/NNNN_<slug>.sql` for the next version, update `schema.sql` to match, then run `apply_database` (dry run first).

## Storage

| Script | What it does | Flags |
| --- | --- | --- |
| `clean_orphans` | Lists every bucket object no database row references and deletes them with `--apply`. It covers chart files `{owner}/{chart}/{hash}`, replays `{owner}/{chart}/replays/{submitter}/{hash}`, and avatars and banners `{account}/profile\|banner/{sha256}` with `_webp`. Dry run by default: it prints missing referenced keys, every orphan key with its size, and totals per category. It always refuses when the database scan fails or finds no charts. With `--apply` it also refuses when the database looks out of sync with the bucket: the newest object is over a day newer than the newest DB write, or over 1% of referenced keys are missing. It asks you to type `delete`, re-checks the database before deleting, and reports per-key delete failures. | `--apply`, `--yes`, `--min-age-minutes N` (default 60: newer unreferenced objects may be uploads in progress), `--skip-unowned` (keep files of charts/accounts that have no row, like the old `find_orphans`), `--include-unrecognized`, `--ignore-staleness`, `--concurrency N` |
| `wipe_s3` | Deletes every object in the bucket. Asks you to type the bucket name. | |

## Accounts and staff

| Script | What it does | Flags |
| --- | --- | --- |
| `ban_account` *constants* | Bans an account, deletes its sessions and revokes its OAuth tokens and codes. By default it also schedules the account for deletion after the grace period: the worker then deletes its content and files but keeps the banned row, so the ban survives a new login. `--no-delete` leaves a deletion that is already pending alone. `--unban` reverses both. Logged as `ban`. | `--id ID` \| `--handle N`, `--unban`, `--no-delete`, `--yes` |
| `delete_account` *constants* | Deletes an account, its files and its rows immediately, using `purge_account` like `DELETE /v1/accounts/{id}`. Logged as `account_delete` once the purge succeeded. | `--id ID` \| `--handle N`, `--yes` |
| `staff_account` *constants* | Lists an account's staff permissions or changes them. `--revoke-all` combined with `--grant`/`--preset` keeps only those. Only real changes are logged, as `permission_grant` / `permission_revoke`. | `--id ID` \| `--handle N`, `--grant PERM`..., `--revoke PERM`..., `--preset mod\|admin`, `--revoke-all` |
| `staff_accounts_bulk` *constants* | Applies the same permission changes to several accounts by handle. | `HANDLE...`, `--file PATH`, plus the `staff_account` change flags |
| `rollback_staff_actions` | Lists the staff actions of one mod (`--handle`) or of the admin key (`--admin-key`) since a time, and after a `y` confirmation rolls them back newest first, deleting each log entry whose rollback changed a row. Entries whose target is gone or already holds the old value are reported as no-ops and kept. Handles `visibility_change`, `comment_delete`, `staff_pick`, `constant_rerate`, `ban` (sets `banned` back and clears `deleted_at`), `delete`, `undelete`, `permission_grant`, `permission_revoke` and `schedule_change` (a previous publish time that has passed is cleared instead of restored, so nothing publishes right away). `account_delete`, `chart_purge` and `leaderboard_delete` can't be reversed and are skipped. | `--handle N` \| `--admin-key`, `--since ISO` (UTC unless it has an offset), `--dry-run` |
| `check_level_range` *constants* | Shows the rating range promotion targeting assumes for an account from its recently opened charts. | `--id ID` \| `--handle N` |

## Content

| Script | What it does | Flags |
| --- | --- | --- |
| `send_notification` *constants* | Sends a notification to an account or to a chart's owner, within the API's length limits. | `--id ID` \| `--handle N` \| `--chart-id ID`, `--title`, `--content` |
| `add_promotion` | Starts a promotion for a public chart that isn't pending deletion. | `--chart-id ID` (`UnCh-` prefix optional), `--views N` \| `--clicks N` |
| `promotion_metrics` | Prints progress, views, clicks and duration of every promotion, or of one. | `--id N` |
| `oauth_app` | Manages third-party OAuth apps. A new or regenerated client secret is printed once and stored hashed. | `create --name --redirect-uri... [--description] [--public] [--owner ID]`, `regenerate --client-id`, `redirect-uris --client-id --redirect-uri...`, `delete --client-id [--yes]`, `list` |

`_common.py` holds the shared helpers (config loading that never echoes values, connections, the admin-key `AppContext`, prompts).
