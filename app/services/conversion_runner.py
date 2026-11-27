import asyncio
import ctypes
import functools
import json
import logging
import os
import subprocess
import sys
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable
from concurrent.futures import Future
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from app.context import AppContext
from app.database.core import Connection
from app.errors import ApiError, bad_request, too_large
from app.services import conversion
from app.settings import ChartsConfig

logger = logging.getLogger("uc.backend.conversion")

ROOT = Path(__file__).resolve().parents[2]
CHILD_MODULE = "app.services.conversion"
SLOT_LOCK_CLASS = 0x5543_4356
SLOT_POLL_SECONDS = 0.25
DISCONNECT_POLL_SECONDS = 1.0
KILL_WAIT_SECONDS = 10.0
STDERR_LOG_CHARS = 2000

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0100
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x0400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100

DisconnectCheck = Callable[[], Awaitable[bool]]


class _BasicLimitInformation(ctypes.Structure):
    """JOBOBJECT_BASIC_LIMIT_INFORMATION of the Windows API.

    Attributes:
        _fields_: Layout of the C structure.
    """

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    """IO_COUNTERS of the Windows API.

    Attributes:
        _fields_: Layout of the C structure.
    """

    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION of the Windows API.

    Attributes:
        _fields_: Layout of the C structure.
    """

    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


@functools.cache
def _kernel32() -> ctypes.CDLL:
    """Load kernel32 with the job object functions typed.

    Returns:
        ctypes.CDLL: The library.
    """
    if sys.platform != "win32":
        raise OSError("job objects only exist on Windows")
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p
    prototypes: dict[str, tuple[type, tuple[type, ...]]] = {
        "CreateJobObjectW": (handle, (ctypes.c_void_p, ctypes.c_wchar_p)),
        "SetInformationJobObject": (
            ctypes.c_int,
            (handle, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32),
        ),
        "AssignProcessToJobObject": (ctypes.c_int, (handle, handle)),
        "OpenProcess": (handle, (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)),
        "CloseHandle": (ctypes.c_int, (handle,)),
    }
    for name, (restype, argtypes) in prototypes.items():
        function = getattr(library, name)
        function.restype = restype
        function.argtypes = argtypes
    return library


def _create_job(pid: int, memory_limit: int) -> int:
    """Put a process in a new job object that caps its memory and kills it when the worker exits.

    Args:
        pid: Process id of the child.
        memory_limit: Largest committed memory the child may use, in bytes.

    Returns:
        int: Handle of the job, to keep open while the child runs.
    """
    if sys.platform != "win32":
        raise OSError("job objects only exist on Windows")
    kernel32 = _kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
            | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        limits.ProcessMemoryLimit = memory_limit
        if not kernel32.SetInformationJobObject(
            job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        process = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel32.AssignProcessToJobObject(job, process):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(process)
    except BaseException:
        kernel32.CloseHandle(job)
        raise
    return job


def _close_job(job: int) -> None:
    """Close a job object handle, killing whatever still runs in it.

    Args:
        job: Handle from ``_create_job``.
    """
    if sys.platform == "win32":
        _kernel32().CloseHandle(job)


def _kill(process: subprocess.Popen[bytes]) -> None:
    """Kill a child process unless it already exited.

    Args:
        process: The child.
    """
    try:
        process.kill()
    except OSError:
        logger.exception("could not kill conversion child %s", process.pid)


class ConversionChild:
    """One conversion child process that any thread may kill, even before it has started.

    Attributes:
        lock: Guards the process and the cancelled flag.
        process: The started process, once there is one.
        cancelled: Whether the child was killed or refused before it finished.
    """

    def __init__(self) -> None:
        """Create a child that has not started yet."""
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.cancelled = False

    def attach(self, process: subprocess.Popen[bytes]) -> bool:
        """Remember the started process unless the child was cancelled meanwhile.

        Args:
            process: The started process.

        Returns:
            bool: Whether the process may run.
        """
        with self.lock:
            self.process = process
            return not self.cancelled

    def cancel(self) -> None:
        """Kill the child now if it runs, or as soon as it starts."""
        with self.lock:
            self.cancelled = True
            process = self.process
        if process is not None:
            _kill(process)


class ConversionChildren:
    """Conversion children a worker is running, so shutdown can kill every one of them.

    Attributes:
        lock: Guards the running set and the closed flag.
        running: Children that have not finished yet.
        closed: Whether the worker is shutting down and refuses new children.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self.lock = threading.Lock()
        self.running: set[ConversionChild] = set()
        self.closed = False

    def track(self, child: ConversionChild) -> bool:
        """Register a child about to start.

        Args:
            child: The child.

        Returns:
            bool: False when the worker is shutting down and the child must not start.
        """
        with self.lock:
            if self.closed:
                return False
            self.running.add(child)
            return True

    def untrack(self, child: ConversionChild) -> None:
        """Forget a finished child.

        Args:
            child: The child.
        """
        with self.lock:
            self.running.discard(child)

    def kill_all(self) -> None:
        """Kill every running child and refuse new ones."""
        with self.lock:
            self.closed = True
            children = list(self.running)
        for child in children:
            child.cancel()


@dataclass
class ChildOutcome:
    """How a conversion child ended.

    Attributes:
        returncode: Exit status, negative for the POSIX signal that ended it.
        stdout: Gzipped level data when it succeeded.
        stderr: Error report or crash output.
        timed_out: Whether it was killed for running too long.
    """

    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


def _interpreter() -> tuple[str, dict[str, str] | None]:
    """Pick the interpreter for a child, bypassing the Windows venv redirector like multiprocessing.

    Returns:
        tuple[str, dict[str, str] | None]: Executable and environment, None to inherit it unchanged.
    """
    base = getattr(sys, "_base_executable", sys.executable)
    if sys.platform != "win32" or os.path.normcase(base) == os.path.normcase(sys.executable):
        return sys.executable, None
    return base, {**os.environ, "__PYVENV_LAUNCHER__": sys.executable}


def _run_child(child: ConversionChild, raw: bytes, settings: ChartsConfig) -> ChildOutcome:
    """Start a conversion child, feed it the chart and wait for it, killing it on timeout.

    Args:
        child: Handle the event loop uses to kill the child.
        raw: Uploaded chart file.
        settings: Chart processing settings.

    Returns:
        ChildOutcome: How the child ended.
    """
    executable, environment = _interpreter()
    command = [
        executable,
        "-m",
        CHILD_MODULE,
        str(settings.max_decompressed_size),
        str(settings.conversion_memory_limit),
        str(os.getpid()),
    ]
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=ROOT,
        env=environment,
    ) as process:
        job: int | None = None
        try:
            if sys.platform == "win32":
                job = _create_job(process.pid, settings.conversion_memory_limit)
            if not child.attach(process):
                return ChildOutcome(returncode=-1, stdout=b"", stderr=b"")
            try:
                stdout, stderr = process.communicate(raw, timeout=settings.conversion_timeout)
            except subprocess.TimeoutExpired:
                _kill(process)
                stdout, stderr = process.communicate()
                return ChildOutcome(process.returncode, stdout, stderr, timed_out=True)
            return ChildOutcome(process.returncode, stdout, stderr)
        finally:
            _kill(process)
            process.wait()
            if job is not None:
                _close_job(job)


def _consume_result(future: asyncio.Future[ChildOutcome]) -> None:
    """Mark a finished future's exception as retrieved so an abandoned wait logs nothing.

    Args:
        future: The finished future.
    """
    if not future.cancelled():
        future.exception()


async def _run_in_child(
    context: AppContext, raw: bytes, disconnected: DisconnectCheck | None
) -> ChildOutcome:
    """Run one conversion child on the thread pool, killing it if the request fails or goes away.

    Args:
        context: App context.
        raw: Uploaded chart file.
        disconnected: Tells whether the client went away, or None to never check.

    Returns:
        ChildOutcome: How the child ended.
    """
    child = ConversionChild()
    children = context.conversion_children
    if not children.track(child):
        raise ApiError(503, "error.chart.conversion_busy")
    try:
        future: Future[ChildOutcome] = context.executor.submit(
            _run_child, child, raw, context.config.charts
        )
        waiter = asyncio.wrap_future(future)
        waiter.add_done_callback(_consume_result)
        poll = DISCONNECT_POLL_SECONDS if disconnected is not None else None
        try:
            while not waiter.done():
                await asyncio.wait({waiter}, timeout=poll)
                if not waiter.done() and disconnected is not None and await disconnected():
                    raise bad_request("error.request.disconnected")
        except BaseException:
            child.cancel()
            await asyncio.wait({waiter}, timeout=KILL_WAIT_SECONDS)
            raise
        outcome = waiter.result()
    finally:
        children.untrack(child)
    if child.cancelled:
        raise ApiError(503, "error.chart.conversion_busy")
    return outcome


async def _lock_free_slot(connection: Connection, slots: int) -> int | None:
    """Take the first free global conversion slot on a session.

    Args:
        connection: Connection that holds the slot's advisory lock.
        slots: Number of slots.

    Returns:
        int | None: The slot taken, or None when every slot is busy.
    """
    for slot in range(slots):
        if await connection.fetchval("SELECT pg_try_advisory_lock($1, $2)", SLOT_LOCK_CLASS, slot):
            return slot
    return None


@asynccontextmanager
async def conversion_slot(
    context: AppContext, disconnected: DisconnectCheck | None = None
) -> AsyncGenerator[None, None]:
    """Hold one of the conversion slots shared by every worker, waiting for one to free up.

    Args:
        context: App context.
        disconnected: Tells whether the client went away, or None to never check.

    Returns:
        AsyncGenerator[None, None]: Context held while the slot is taken.
    """
    settings = context.config.charts
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.conversion_queue_timeout
    while True:
        async with context.database.connection() as connection:
            slot = await _lock_free_slot(connection, settings.conversion_slots)
            if slot is not None:
                try:
                    yield
                finally:
                    await connection.fetchval(
                        "SELECT pg_advisory_unlock($1, $2)", SLOT_LOCK_CLASS, slot
                    )
                return
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise ApiError(503, "error.chart.conversion_busy")
        if disconnected is not None and await disconnected():
            raise bad_request("error.request.disconnected")
        await asyncio.sleep(min(SLOT_POLL_SECONDS, remaining))


def _reported_error(stderr: bytes) -> ApiError | None:
    """Read the error a child reported as the last line of its stderr.

    Args:
        stderr: The child's stderr.

    Returns:
        ApiError | None: The reported error, or None when the report is missing or malformed.
    """
    lines = stderr.decode("utf-8", "replace").strip().splitlines()
    if not lines:
        return None
    try:
        report = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(report, dict):
        return None
    code, status, params = report.get("code"), report.get("status"), report.get("params")
    if not isinstance(code, str) or not isinstance(params, dict):
        return None
    if not isinstance(status, int) or not 400 <= status < 500:
        return None
    return ApiError(status, code, params)


def _level_data(outcome: ChildOutcome, settings: ChartsConfig) -> bytes:
    """Turn a finished child into its level data, or raise the error it ended with.

    Args:
        outcome: How the child ended.
        settings: Chart processing settings.

    Returns:
        bytes: Gzipped next_sekai level data.
    """
    if outcome.timed_out:
        logger.warning("chart conversion killed after %s seconds", settings.conversion_timeout)
        raise too_large("error.chart.conversion_timed_out", seconds=settings.conversion_timeout)
    if outcome.returncode == 0 and outcome.stdout:
        return outcome.stdout
    if outcome.returncode == conversion.EXIT_OUT_OF_MEMORY:
        raise too_large("error.chart.conversion_too_large", limit=settings.conversion_memory_limit)
    if outcome.returncode == conversion.EXIT_REJECTED:
        reported = _reported_error(outcome.stderr)
        if reported is not None:
            raise reported
    logger.error(
        "chart conversion child exited with %s\n%s",
        outcome.returncode,
        outcome.stderr.decode("utf-8", "replace")[-STDERR_LOG_CHARS:],
    )
    raise bad_request("error.chart.conversion_failed")


async def convert_chart(
    context: AppContext, raw: bytes, disconnected: DisconnectCheck | None = None
) -> bytes:
    """Convert an uploaded chart to gzipped next_sekai level data in a child process.

    Args:
        context: App context.
        raw: Uploaded chart file.
        disconnected: Tells whether the client went away, or None to never check.

    Returns:
        bytes: Gzipped next_sekai level data.
    """
    async with conversion_slot(context, disconnected):
        outcome = await _run_in_child(context, raw, disconnected)
    return _level_data(outcome, context.config.charts)
