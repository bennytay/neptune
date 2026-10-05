"""A decoder run on hostile bytes in a child process under an OS memory cap and a time limit.

What a library decodes from a file is bounded by what the file states only as far as the library
checks those statements; a forged one can make it allocate far past any limit (ADR 0014 §5). So
such a decoder runs in a process whose private writable memory the kernel caps (``RLIMIT_DATA``)
at what it held when it started plus ``memory`` bytes, and which is killed after ``seconds``. It
reads the bytes through the caller, which serves them from its verified reader, so a chunk is
still hashed before any byte of it is decoded.

A **worker** (``python -m neptune_ledger.lake.capped``, a clean interpreter that has imported the
decoders) forks one capped child per call, so a call costs a fork, not an interpreter start, and
no memory or state is carried from one call to the next. A call takes an idle worker or starts
one; a worker whose call ran past its time is killed (the caller's own child, by its handle), and
its capped child dies with it (``PR_SET_PDEATHSIG``). Linux only: elsewhere ``run`` refuses.
"""

import atexit
import ctypes
import io
import os
import pickle
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal

# What a worker imports before it forks any child.
_PRELOAD: Final = ("neptune_ledger.lake.decode",)
_PR_SET_PDEATHSIG: Final = 1
# A worker forks, so it must hold one thread: importing pyarrow otherwise starts BLAS threads and
# a jemalloc background thread. The system allocator is what ``RLIMIT_DATA`` measures plainly.
_SINGLE_THREADED: Final = {
    "ARROW_DEFAULT_MEMORY_POOL": "system",
    "JE_ARROW_MALLOC_CONF": "background_thread:false",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
}


@dataclass(frozen=True)
class Exceeded:
    """Why a capped call returned nothing: its memory cap was hit (``memory``), it ran past its
    time (``time``), it ended without a result (``died``: killed, or out of memory where the
    allocator could not report it), or this platform cannot cap a child (``unsupported``)."""

    reason: Literal["died", "memory", "time", "unsupported"]
    detail: str


def supported() -> bool:
    """Whether a child's memory can be capped here: Linux, where ``RLIMIT_DATA`` counts every
    private writable mapping (since 4.7), so an allocator's ``mmap`` is refused past it."""
    if sys.platform != "linux":
        return False
    try:
        import resource
    except ImportError:  # pragma: no cover - every Linux CPython has it
        return False
    return hasattr(resource, "RLIMIT_DATA")


class Remote:
    """The bytes a capped call decodes, read through the caller: ``size`` and a seekable file."""

    def __init__(self, conn: Connection, size: int) -> None:
        self._conn, self.size = conn, size

    def file(self) -> io.BufferedReader:
        return io.BufferedReader(_RemoteFile(self._conn, self.size), buffer_size=1 << 20)

    def head(self, n: int) -> bytes:
        with self.file() as f:
            return f.read(n)

    def tail(self, n: int) -> bytes:
        with self.file() as f:
            f.seek(max(0, self.size - n))
            return f.read(n)


class _RemoteFile(io.RawIOBase):
    def __init__(self, conn: Connection, size: int) -> None:
        self._conn, self._size, self._at = conn, size, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._at

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._at, io.SEEK_END: self._size}[whence]
        if base + offset < 0:
            raise OSError("seek before the start")
        self._at = base + offset
        return self._at

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        n = max(0, min(len(view), self._size - self._at))
        if n == 0:
            return 0
        self._conn.send(("read", self._at, n))
        data = self._conn.recv_bytes()
        view[: len(data)] = data
        self._at += len(data)
        return len(data)


# --- The caller's side -------------------------------------------------------------------------


class _Worker:
    def __init__(self) -> None:
        ours, theirs = socket.socketpair()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "neptune_ledger.lake.capped", str(theirs.fileno())],
            pass_fds=(theirs.fileno(),),
            stdin=subprocess.DEVNULL,
            # The modules this process imports, wherever it found them.
            env={
                **os.environ,
                **_SINGLE_THREADED,
                "PYTHONPATH": os.pathsep.join(filter(None, sys.path)),
            },
        )
        theirs.close()
        self.conn = Connection(ours.detach())

    def close(self) -> None:
        self.conn.close()
        if self.process.poll() is None:
            self.process.kill()  # the worker this module started, by its own handle
        self.process.wait()


_idle: list[_Worker] = []
_lock = threading.Lock()


def _take() -> _Worker:
    with _lock:
        while _idle:
            worker = _idle.pop()
            if worker.process.poll() is None:
                return worker
            worker.close()
    return _Worker()


@atexit.register
def _close_idle() -> None:
    with _lock:
        while _idle:
            _idle.pop().close()


def run(
    function: Callable[..., Any],
    args: tuple[Any, ...],
    source: BinaryIO,
    size: int,
    *,
    memory: int,
    seconds: float,
) -> Any:
    """``function(Remote, *args)`` in a capped child, ``source`` (``size`` bytes) served to it.

    ``function`` must be importable by name, and it, ``args`` and its result must pickle.
    Returns the result, or ``Exceeded``. An exception the caller raises serving a read (a
    changed chunk) propagates, after the worker and its child are killed.
    """
    if not supported():
        return Exceeded("unsupported", f"{sys.platform} offers no memory cap for a child process")
    worker = _take()
    reusable = False
    try:
        outcome, reusable = _call(worker, (function, args, size, memory), source, memory, seconds)
        return outcome
    finally:
        if reusable:
            with _lock:
                _idle.append(worker)
        else:
            worker.close()


def _call(
    worker: _Worker, request: tuple[Any, ...], source: BinaryIO, memory: int, seconds: float
) -> tuple[Any, bool]:
    """Serve one call's reads until the worker says how its child ended. A watchdog kills the
    worker at the deadline, so a read of the connection never blocks past it."""
    conn = worker.conn
    late = threading.Event()

    def overdue() -> None:
        late.set()
        worker.process.kill()  # the worker this module started, by its own handle

    watchdog = threading.Timer(seconds, overdue)
    watchdog.daemon = True
    watchdog.start()
    outcome: Any = None
    try:
        conn.send(request)
        while True:
            message = conn.recv()
            if message[0] == "read":
                source.seek(message[1])
                conn.send_bytes(source.read(message[2]))
            elif message[0] == "done":
                outcome = message[1]
            elif message[0] == "memory":
                outcome = Exceeded("memory", f"decoding needed more than its {memory}-byte cap")
            elif message[0] == "refused":
                outcome = Exceeded("unsupported", f"no memory cap could be set: {message[1]}")
            elif outcome is None:  # "exit": the child ended without a result
                detail = (
                    f"the decoding process ended ({message[1]}) without a result, under its"
                    f" {memory}-byte memory cap"
                )
                return Exceeded("died", detail), True
            else:
                return outcome, True
    except (EOFError, OSError, pickle.UnpicklingError):
        if late.is_set():
            return Exceeded("time", f"decoding ran past its {seconds:g}-second limit"), False
        detail = f"the decoding worker ended without a result, under its {memory}-byte memory cap"
        return Exceeded("died", detail), False
    finally:
        watchdog.cancel()


# --- The worker and its capped children --------------------------------------------------------


def _die_with_parent() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


def _serve(fd: int) -> None:
    """The worker: one capped child per request; it tells the caller how each child ended."""
    _die_with_parent()
    parent = os.getppid()
    if parent == 1:
        return
    for name in _PRELOAD:
        __import__(name)
    conn = Connection(fd)
    while True:
        try:
            function, args, size, memory = conn.recv()
        except EOFError:
            return
        child = os.fork()
        if child == 0:
            try:
                _die_with_parent()
                _capped(conn, function, args, size, memory)
            finally:
                os._exit(0)
        _, status = os.waitpid(child, 0)
        if os.WIFSIGNALED(status):
            ended = f"signal {signal.Signals(os.WTERMSIG(status)).name}"
        else:
            ended = f"exit {os.waitstatus_to_exitcode(status)}"
        conn.send(("exit", ended))


def _capped(
    conn: Connection, function: Callable[..., Any], args: tuple[Any, ...], size: int, memory: int
) -> None:
    import resource

    try:
        cap = _data_bytes() + memory
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_DATA, (cap, cap))
    except (OSError, ValueError) as exc:
        conn.send(("refused", f"{type(exc).__name__}: {exc}"))
        return
    exceeded = False
    try:
        result = function(Remote(conn, size), *args)
    except MemoryError:
        exceeded = True  # sent once the handler has dropped the frames that held the memory
    conn.send(("memory",) if exceeded else ("done", result))


def _data_bytes() -> int:
    """This process's private writable memory, as ``RLIMIT_DATA`` counts it (``VmData``)."""
    with Path("/proc/self/status").open("rb") as status:
        for line in status:
            if line.startswith(b"VmData:"):
                return int(line.split()[1]) * 1024
    raise OSError("no VmData in /proc/self/status")


if __name__ == "__main__":
    _serve(int(sys.argv[1]))
