import argparse
import asyncio
import mimetypes
from pathlib import Path

from app import public_assets
from app.services.storage import Storage
from scripts._common import add_config_argument, format_bytes, read_config

mimetypes.add_type("image/webp", ".webp")


def local_files() -> dict[str, Path]:
    """List the files in the assets folder.

    Returns:
        dict[str, Path]: Files keyed by their storage key.
    """
    root = public_assets.DIRECTORY
    if not root.is_dir():
        raise SystemExit(f"{root} doesn't exist")
    return {
        public_assets.storage_key(path.relative_to(root).as_posix()): path
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def content_type(path: Path) -> str:
    """Guess a file's MIME type.

    Args:
        path: File.

    Returns:
        str: The type, ``application/octet-stream`` when unknown.
    """
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


async def run(args: argparse.Namespace) -> None:
    """Mirror the assets folder to storage.

    Args:
        args: Parsed command line.
    """
    config = read_config(args.config)
    files = local_files()
    storage = Storage(config.storage)
    await storage.open()
    try:
        remote = set(await storage.list_keys(public_assets.STORAGE_PREFIX))
        stale = sorted(remote - set(files))
        total = sum(path.stat().st_size for path in files.values())
        print(f"Upload {len(files)} file(s) ({format_bytes(total)}):")
        for key, path in files.items():
            print(f"  {key}  {format_bytes(path.stat().st_size)}")
        print(f"Delete {len(stale)} file(s) no longer in assets/:")
        for key in stale:
            print(f"  {key}")
        if not args.apply:
            print("\nDry run, nothing was changed. Re-run with --apply to upload.")
            return
        await storage.put_many(
            (key, path.read_bytes(), content_type(path)) for key, path in files.items()
        )
        if stale:
            await storage.delete_keys(stale)
        print(f"\nUploaded {len(files)} and deleted {len(stale)} file(s).")
    finally:
        await storage.close()


def main() -> None:
    """Parse the command line and mirror the assets folder to storage."""
    parser = argparse.ArgumentParser(
        description=(
            "Upload everything in assets/ to storage under assets/, replacing existing files and "
            "deleting files that are no longer in the folder. Dry run by default."
        )
    )
    add_config_argument(parser)
    parser.add_argument("--apply", action="store_true", help="upload and delete")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
