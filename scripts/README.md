# Scripts

Run from the repo root: `python -m scripts.<name>`.

- All take `--config` (default `config.yml`); *constants* ones also take `--constants`.
- Staff changes are logged like `X-Admin-Key` API calls.
- Destructive scripts ask for confirmation; `--apply` scripts dry-run by default.

## Database

| Script | Does | Flags |
| --- | --- | --- |
| `database_setup` | Create a fresh database from `schema.sql` | |
| `apply_database` | Show or run pending migrations (version 5 or newer) | `--apply` |
| `db_dump` | Dump to plain SQL | `[output_file]`, `--pg-bin DIR` |
| `db_restore` | Wipe and restore a dump | `input_file`, `--pg-bin DIR` |

New migration: add `migrations/NNNN_<slug>.sql`, update `schema.sql`, run `apply_database`.

## Storage

| Script | Does | Flags |
| --- | --- | --- |
| `clean_orphans` | Delete bucket files no row uses (skips `og/` and `assets/`) | `--apply`, `--yes`, `--min-age-minutes N`, `--skip-unowned`, `--include-unrecognized`, `--ignore-staleness`, `--concurrency N` |
| `upload_assets` | Mirror `assets/` to the bucket: upload every file, delete files no longer there | `--apply` |
| `wipe_s3` | Delete every file in the bucket | |

## Accounts and staff

| Script | Does | Flags |
| --- | --- | --- |
| `ban_account` *constants* | Ban (and by default schedule deletion), or unban | `--id ID` \| `--handle N`, `--unban`, `--no-delete`, `--yes` |
| `delete_account` *constants* | Delete an account and its files now | `--id ID` \| `--handle N`, `--yes` |
| `staff_account` *constants* | List or change staff permissions | `--id ID` \| `--handle N`, `--grant PERM`..., `--revoke PERM`..., `--preset mod\|admin`, `--revoke-all` |
| `staff_accounts_bulk` *constants* | Same permission change for many accounts | `HANDLE...`, `--file PATH`, plus `staff_account` flags |
| `account_badge` *constants* | List, give or remove badges (`owner`, `developer`) | `--id ID` \| `--handle N`, `--grant BADGE`, `--remove BADGE`, `--level N` |
| `rollback_staff_actions` | Undo a mod's or the admin key's actions since a time | `--handle N` \| `--admin-key`, `--since ISO`, `--dry-run` |
| `check_level_range` *constants* | Show the rating range promotions target for an account | `--id ID` \| `--handle N` |

## Content

| Script | Does | Flags |
| --- | --- | --- |
| `send_notification` *constants* | Notify an account or a chart's owner | `--id ID` \| `--handle N` \| `--chart-id ID`, `--title`, `--content` |
| `add_promotion` | Promote a public chart | `--chart-id ID`, `--views N` \| `--clicks N` |
| `promotion_metrics` | Show promotion stats | `--id N` |
| `delete_short_charts` *constants* | Soft-delete charts with songs under `audio.min_music_seconds` (any visibility) and public or scheduled charts under `charts.min_public_chart_seconds` (needs `ffprobe`) | `--apply`, `--yes`, `--concurrency N` |
| `backfill_chart_seconds` | Measure and store the length of charts that have none, so making them public doesn't measure them first (safe while live, rerunnable) | `--apply`, `--concurrency N` |
| `generate_missing_previews` | Cut 30 s previews for public/scheduled charts without one (needs `ffmpeg`) | `--apply`, `--workers N`, `--concurrency N`, `--batch-size N`, `--limit N`, `--show N` |
| `oauth_app` | Manage OAuth apps | `create`, `regenerate`, `redirect-uris`, `delete`, `list` |

Shared helpers live in `_common.py`.
