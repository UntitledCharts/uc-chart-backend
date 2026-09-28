# Database Migrations

`scripts/database_setup.py` is meant to only create a new database at the latest schema with the latest version included, and not for migration. To add a migration, create a `migrate_N_to_N+1.py` script, add it in `migrate.py`, and bump `LATEST_SCHEMA_VERSION` in `database_setup.py`.

For new tables just use the setup script (`python -m scripts.database_setup`) and not a migration.

### Run
In the root directory, run
```
python -m scripts.database_migration.migrate
```
