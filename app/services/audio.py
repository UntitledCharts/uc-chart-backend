import json
import os
import subprocess
import tempfile

from app.settings import AudioConstants


def _probe_packets(path: str, constants: AudioConstants) -> list[tuple[float, int]]:
    """Read packet durations and sizes from the start of an MP3.

    Args:
        path: File to probe.
        constants: Audio constants.

    Returns:
        list[tuple[float, int]]: Duration and size of each sampled packet.
    """
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-protocol_whitelist",
            "file",
            "-f",
            "mp3",
            "-select_streams",
            "a:0",
            "-show_entries",
            "packet=duration_time,size",
            "-read_intervals",
            f"%+{constants.vbr_probe_seconds}",
            "-print_format",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=constants.probe_timeout_seconds,
    )
    if result.returncode != 0:
        return []
    packets = json.loads(result.stdout).get("packets", [])
    return [
        (float(packet.get("duration_time", 0)), int(packet.get("size", 0))) for packet in packets
    ]


def is_vbr(path: str, constants: AudioConstants) -> bool:
    """Check whether an MP3 uses a variable bitrate.

    Args:
        path: File to check.
        constants: Audio constants.

    Returns:
        bool: Whether the sampled bitrates spread more than the tolerance.
    """
    try:
        packets = _probe_packets(path, constants)
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
        ValueError,
    ):
        return False
    if len(packets) < 2:
        return False
    bitrates = [size * 8 / duration for duration, size in packets if duration > 0]
    if not bitrates or min(bitrates) <= 0:
        return False
    return (max(bitrates) - min(bitrates)) / min(bitrates) > constants.vbr_tolerance


def _average_bitrate(path: str, constants: AudioConstants) -> int:
    """Read an MP3's average bitrate.

    Args:
        path: File to probe.
        constants: Audio constants.

    Returns:
        int: Average bitrate, or the default when it can't be read.
    """
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-protocol_whitelist",
            "file",
            "-f",
            "mp3",
            "-print_format",
            "json",
            "-show_format",
            path,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=constants.probe_timeout_seconds,
    )
    if result.returncode != 0:
        return constants.default_bitrate
    return int(
        json.loads(result.stdout).get("format", {}).get("bit_rate", constants.default_bitrate)
    )


def ensure_cbr_mp3(audio: bytes, constants: AudioConstants) -> bytes:
    """Re-encode a VBR MP3 to the nearest standard constant bitrate.

    Args:
        audio: MP3 file.
        constants: Audio constants.

    Returns:
        bytes: The CBR file, or the original when it already is CBR or re-encoding fails.
    """
    input_path = output_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
            handle.write(audio)
            input_path = handle.name
        if not is_vbr(input_path, constants):
            return audio
        average = _average_bitrate(input_path, constants)
        bitrate = min(constants.standard_bitrates, key=lambda candidate: abs(candidate - average))
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
            output_path = handle.name
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-protocol_whitelist",
                "file",
                "-f",
                "mp3",
                "-i",
                input_path,
                "-codec:a",
                "libmp3lame",
                "-b:a",
                str(bitrate),
                output_path,
            ],
            capture_output=True,
            timeout=constants.encode_timeout_seconds,
        )
        if result.returncode != 0:
            return audio
        with open(output_path, "rb") as handle:
            return handle.read()
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        json.JSONDecodeError,
        ValueError,
    ):
        return audio
    finally:
        for path in (input_path, output_path):
            if path and os.path.exists(path):
                os.unlink(path)
