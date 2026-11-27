from fastapi import UploadFile

from app.errors import too_large

CHUNK_SIZE = 1024 * 1024


async def read_limited(upload: UploadFile, limit: int, field: str) -> bytes:
    """Read an uploaded file without accepting more than a size limit.

    Args:
        upload: Uploaded file.
        limit: Largest accepted size in bytes.
        field: Form field name, reported in the error.

    Returns:
        bytes: File contents.
    """
    if upload.size is not None and upload.size > limit:
        raise too_large("error.file.too_large", field=field, limit=limit)
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise too_large("error.file.too_large", field=field, limit=limit)
        chunks.append(chunk)
    return b"".join(chunks)
