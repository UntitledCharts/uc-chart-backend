import contextlib
import ctypes
import gzip
import io
import json
import os
import signal
import sys
import zlib
from typing import Any

GZIP_MAGIC = b"\x1f\x8b"
ZIP_MAGIC = b"PK\x03\x04"
EXIT_REJECTED = 3
EXIT_OUT_OF_MEMORY = 4
EXIT_ORPHANED = 5
PR_SET_PDEATHSIG = 1


class ConversionError(Exception):
    """A chart the converter refuses, reported to the client as an error code.

    Attributes:
        status: HTTP status of the error.
        code: Machine-readable error code such as ``error.chart.invalid_format``.
        params: Values the client may need to render the error.
    """

    def __init__(self, status: int, code: str, **params: Any) -> None:
        """Create a conversion error.

        Args:
            status: HTTP status of the error.
            code: Machine-readable error code.
            **params: Values the client may need to render the error.
        """
        super().__init__(code)
        self.status = status
        self.code = code
        self.params = params

    def to_json(self) -> str:
        """Serialize the error the way the child reports it on stderr.

        Returns:
            str: One line of JSON with the code, status and params.
        """
        return json.dumps({"code": self.code, "status": self.status, "params": self.params})


def inflate_limited(data: bytes, limit: int) -> bytes:
    """Decompress gzip or zlib data without ever exceeding a size limit.

    Args:
        data: Compressed data.
        limit: Largest decompressed size accepted.

    Returns:
        bytes: Decompressed data.
    """
    decompressor = zlib.decompressobj(47)
    try:
        output = decompressor.decompress(data, limit + 1)
    except zlib.error:
        raise ConversionError(400, "error.file.corrupt") from None
    if len(output) > limit or decompressor.unconsumed_tail:
        raise ConversionError(413, "error.file.decompressed_too_large", limit=limit)
    return output


def gzip_level_data(level_data: bytes) -> bytes:
    """Gzip level data deterministically.

    Args:
        level_data: Level data JSON.

    Returns:
        bytes: Gzipped data with a fixed name and timestamp.
    """
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="LevelData", mtime=0) as handle:
        handle.write(level_data)
    return buffer.getvalue()


def normalize_level_data(raw: bytes, limit: int) -> bytes:
    """Re-encode next_sekai level data as compact gzipped JSON.

    Args:
        raw: Uploaded level data, gzipped or plain JSON.
        limit: Largest decompressed size accepted.

    Returns:
        bytes: Gzipped compact JSON.
    """
    text = inflate_limited(raw, limit) if raw[:2] == GZIP_MAGIC else raw
    if len(text) > limit:
        raise ConversionError(413, "error.file.decompressed_too_large", limit=limit)
    try:
        parsed = json.loads(text.decode("utf-8-sig"))
        if not isinstance(parsed, dict):
            raise ConversionError(400, "error.chart.invalid_level_data")
        compact = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False).encode()
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ConversionError(400, "error.chart.invalid_level_data") from None
    return gzip_level_data(compact)


def convert_chart(raw: bytes, limit: int) -> bytes:
    """Detect an uploaded chart's format and convert it to gzipped next_sekai level data.

    Args:
        raw: Uploaded chart file.
        limit: Largest decompressed size accepted.

    Returns:
        bytes: Gzipped next_sekai level data.
    """
    import rg_converters

    try:
        formats = rg_converters.detect(raw, max_decompressed_size=limit)
    except rg_converters.DecompressedSizeError:
        raise ConversionError(413, "error.file.decompressed_too_large", limit=limit) from None
    except ValueError:
        formats = []
    if not formats:
        raise ConversionError(400, "error.chart.invalid_format")
    detected = formats[0]
    next_sekai = rg_converters.format.sonolus_leveldata.next_sekai
    try:
        if detected == "next_sekai":
            if raw[:4] == ZIP_MAGIC:
                score = next_sekai.loads(raw, next_sekai.ImportOptions(max_decompressed_size=limit))
                converted, _ = next_sekai.dumps(
                    score, next_sekai.ExportOptions(max_decompressed_size=limit)
                )
                return converted
            return normalize_level_data(raw, limit)
        if detected in ("sus", "pjsk"):
            source = rg_converters.format.get(detected)
            if detected == "pjsk":
                score = source.loads(raw, source.ImportOptions(max_decompressed_size=limit))
            else:
                score = source.loads(raw)
            converted, _ = next_sekai.dumps(
                score,
                next_sekai.ExportOptions(
                    smooth_guide_fade=True,
                    use_guide_layer=True,
                    max_decompressed_size=limit,
                ),
            )
            return converted
        if detected == "usc":
            score = rg_converters.format.usc.loads(raw)
            converted, _ = next_sekai.dumps(
                score, next_sekai.ExportOptions(max_decompressed_size=limit)
            )
            return converted
    except rg_converters.DecompressedSizeError:
        raise ConversionError(413, "error.file.decompressed_too_large", limit=limit) from None
    except (ValueError, KeyError, RuntimeError) as error:
        raise ConversionError(
            400, "error.chart.conversion_failed", format=detected, detail=str(error)
        ) from None
    if detected in ("chart_cyanvas", "untitled_sekai"):
        raise ConversionError(400, "error.chart.wrong_level_data", format=detected)
    raise ConversionError(400, "error.chart.unsupported_format", format=detected)


def confine(memory_limit: int, parent_pid: int) -> None:
    """Tie this child to its parent's life, cap its memory and make it the first OOM victim on Linux.

    Args:
        memory_limit: Largest address space the child may use, in bytes.
        parent_pid: Process id of the worker that started this child.
    """
    if not sys.platform.startswith("linux"):
        return
    import resource

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, ctypes.c_ulong(signal.SIGKILL), 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"prctl(PR_SET_PDEATHSIG) failed: {os.strerror(error)}")
    if os.getppid() != parent_pid:
        os._exit(EXIT_ORPHANED)
    with contextlib.suppress(OSError), open("/proc/self/oom_score_adj", "w") as handle:
        handle.write("1000")
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    soft = memory_limit if hard == resource.RLIM_INFINITY else min(memory_limit, hard)
    resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


def main() -> None:
    """Convert the chart on stdin to gzipped level data on stdout, as a conversion child."""
    limit, memory_limit, parent_pid = (int(argument) for argument in sys.argv[1:4])
    confine(memory_limit, parent_pid)
    try:
        level_data = convert_chart(sys.stdin.buffer.read(), limit)
    except ConversionError as error:
        sys.stderr.write(error.to_json() + "\n")
        sys.stderr.flush()
        sys.exit(EXIT_REJECTED)
    except MemoryError:
        os._exit(EXIT_OUT_OF_MEMORY)
    sys.stdout.buffer.write(level_data)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
