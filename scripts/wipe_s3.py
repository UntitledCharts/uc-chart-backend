import argparse
import asyncio

from app.services.storage import DELETE_BATCH_SIZE, Storage
from scripts._common import add_config_argument, confirm, format_bytes, read_config


async def delete_s3_objects(config_path: str) -> None:
    """Delete every object in the configured bucket after a typed confirmation.

    Args:
        config_path: Location of config.yml.
    """
    config = read_config(config_path)
    bucket_name = config.storage.bucket
    storage = Storage(config.storage)
    await storage.open()
    try:
        print(f"Listing bucket {bucket_name}...")
        objects = await storage.list_objects()
        if not objects:
            print("No objects found in the bucket.")
            return
        total = sum(size for _, size in objects)
        print(f"{len(objects)} objects, {format_bytes(total)}.")
        print("WARNING: this deletes EVERY object in the bucket and can't be undone.")
        if not confirm(f"Type the bucket name ({bucket_name}) to continue:", bucket_name):
            print("Aborted.")
            return

        keys = [key for key, _ in objects]
        for start in range(0, len(keys), DELETE_BATCH_SIZE):
            batch = keys[start : start + DELETE_BATCH_SIZE]
            await storage.delete_keys(batch)
            print(f"  deleted {start + len(batch)}/{len(keys)}...")
        remaining = len(await storage.list_keys())
        print(f"Deleted {len(keys) - remaining} objects, {remaining} remain.")
    finally:
        await storage.close()


def main() -> None:
    """Parse the command line and wipe the bucket."""
    parser = argparse.ArgumentParser(
        description="Delete every object in the configured storage bucket (asks for its name)."
    )
    add_config_argument(parser)
    asyncio.run(delete_s3_objects(parser.parse_args().config))


if __name__ == "__main__":
    main()
