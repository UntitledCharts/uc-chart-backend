import hashlib
from functools import lru_cache
from pathlib import Path

from app.models import schemas
from app.services.storage import Storage

DIRECTORY = Path(__file__).resolve().parent.parent / "assets"
STORAGE_PREFIX = "assets/"
BANNER = "banner.png"
BANNER_WEBP = "banner.webp"


def storage_key(path: str) -> str:
    """Storage key of a public asset.

    Args:
        path: Path inside the assets folder, with forward slashes.

    Returns:
        str: The key.
    """
    return STORAGE_PREFIX + path


@lru_cache(maxsize=256)
def file_hash(path: str) -> str | None:
    """Hash a public asset as shipped with this version.

    Args:
        path: Path inside the assets folder, with forward slashes.

    Returns:
        str | None: SHA-1 hex of the file, or None when it doesn't exist.
    """
    file = DIRECTORY / path
    if not file.is_file():
        return None
    return hashlib.sha1(file.read_bytes()).hexdigest()


def file_ref(storage: Storage, path: str) -> schemas.FileRef | None:
    """Build the public reference of an asset uploaded with scripts.upload_assets.

    Args:
        storage: Storage client.
        path: Path inside the assets folder, with forward slashes.

    Returns:
        schemas.FileRef | None: Hash and URL, the URL carrying the hash so caches refresh when
            the file changes; None when the asset doesn't exist.
    """
    digest = file_hash(path)
    if digest is None:
        return None
    return schemas.FileRef(
        hash=digest, url=f"{storage.public_url(storage_key(path))}?v={digest[:16]}"
    )
