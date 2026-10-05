"""A decoder run on hostile bytes in a child process under an OS memory cap and a time limit.

What a library decodes from a file is bounded by what the file states only as far as the library
checks those statements; a forged one can make it allocate far past any limit (ADR 0014 §5). So
such a decoder runs in a process whose private writable memory the kernel caps (``RLIMIT_DATA``)
at what it held when it started plus ``memory`` bytes, and which is killed after ``seconds``. It
reads the bytes through the caller, which serves them from its verified reader, so a chunk is
still hashed before any byte of it is decoded.

A **worker** (``python -I capped.py``, a clean interpreter that has imported the decoders, with
a minimal environment and no descriptor but its socket) forks one capped child per call, so a
call costs a fork, not an interpreter start, and no memory or state is carried from one call to
the next. A call takes an idle worker or starts one; a worker is returned only after a call that
ended cleanly, and any other is killed (the caller's own child, by its handle), its capped child
with it (``PR_SET_PDEATHSIG``). A worker exits by itself when the caller's process goes. Linux
only: elsewhere ``run`` refuses.
"""

import atexit
import contextlib
import ctypes
import io
import os
import pickle
import select
import signal
import socket
import subprocess
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal, NoReturn

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
        self.replies = 0  # the caller's replies read in full, so none is left in the socket

    def file(self) -> io.BufferedReader:
        return io.BufferedReader(_RemoteFile(self), buffer_size=1 << 20)

    def head(self, n: int) -> bytes:
        with self.file() as f:
            return f.read(n)

    def tail(self, n: int) -> bytes:
        with self.file() as f:
            f.seek(max(0, self.size - n))
            return f.read(n)


class _RemoteFile(io.RawIOBase):
    def __init__(self, remote: Remote) -> None:
        self._remote, self._size, self._at = remote, remote.size, 0

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
        self._remote._conn.send(("read", self._at, n))
        data = self._remote._conn.recv_bytes()
        self._remote.replies += 1
        view[: len(data)] = data
        self._at += len(data)
        return len(data)


# --- The caller's side -------------------------------------------------------------------------


class _Worker:
    def __init__(self) -> None:
        ours, theirs = socket.socketpair()
        try:
            self.process = subprocess.Popen(
                # Isolated (``-I``): no ``PYTHON*`` variable, user site or script directory.
                [sys.executable, "-I", __file__, str(theirs.fileno()), str(os.getpid())],
                pass_fds=(theirs.fileno(),),  # every other descriptor is closed
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_environment(),
            )
        except BaseException:
            ours.close()
            raise
        finally:
            theirs.close()
        self.conn = Connection(ours.detach())
        # The modules this process imports, wherever it found them, sent rather than inherited.
        # A worker gone already is found by the call, as the end of its socket.
        with contextlib.suppress(OSError):
            self.conn.send([entry for entry in sys.path if entry])

    def close(self) -> None:
        self.conn.close()
        if self.process.poll() is None:
            self.process.kill()  # the worker this module started, by its own handle
        self.process.wait()


def _environment() -> dict[str, str]:
    """All a worker inherits: where programs are, a locale, and few glibc arenas."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C.UTF-8",
        "MALLOC_ARENA_MAX": "2",
    }


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
    worker at the deadline, so a read of the connection never blocks past it.

    The worker is reusable only when its child sent a result having read every reply, then
    exited cleanly, and nothing is left in the socket; after anything else it is discarded."""
    conn = worker.conn
    late = threading.Event()

    def overdue() -> None:
        late.set()
        worker.process.kill()  # the worker this module started, by its own handle

    watchdog = threading.Timer(seconds, overdue)
    watchdog.daemon = True
    watchdog.start()
    finished: tuple[Any, ...] | None = None
    outcome: Any = None
    replies = 0
    try:
        conn.send(request)
        while True:
            message = conn.recv()
            kind = _kind(message)
            if kind == "read" and finished is None:
                source.seek(message[1])
                conn.send_bytes(source.read(message[2]))
                replies += 1
            elif kind == "done" and finished is None:
                finished, outcome = message, message[1]
            elif kind == "memory" and finished is None:
                finished = message
                outcome = Exceeded("memory", f"decoding needed more than its {memory}-byte cap")
            elif kind == "refused" and finished is None:
                finished = message
                outcome = Exceeded("unsupported", f"no memory cap could be set: {message[1]}")
            elif kind == "exit" and finished is None:  # the child ended without a result
                detail = (
                    f"the decoding process ended ({message[1]}) without a result, under its"
                    f" {memory}-byte memory cap"
                )
                return Exceeded("died", detail), False
            elif kind == "exit" and finished is not None:
                clean = finished[0] == "done" and finished[2] == replies and message[2] is True
                return outcome, clean
            else:
                detail = f"the decoding worker broke its protocol, under its {memory}-byte cap"
                return Exceeded("died", detail), False
    except (EOFError, OSError, pickle.UnpicklingError):
        if late.is_set():
            return Exceeded("time", f"decoding ran past its {seconds:g}-second limit"), False
        detail = f"the decoding worker ended without a result, under its {memory}-byte memory cap"
        return Exceeded("died", detail), False
    finally:
        watchdog.cancel()


# Each message a worker or its child sends: its kind and the types of what follows it.
_MESSAGES: Final[dict[str, tuple[type, ...]]] = {
    "read": (int, int),
    "done": (object, int),
    "memory": (int,),
    "refused": (str,),
    "exit": (str, bool),
}


def _kind(message: object) -> str | None:
    """The kind of a well-formed message, or None."""
    if not isinstance(message, tuple) or not message or not isinstance(message[0], str):
        return None
    shape = _MESSAGES.get(message[0])
    if shape is None or len(message) != 1 + len(shape):
        return None
    if not all(isinstance(field, kind) for field, kind in zip(message[1:], shape, strict=True)):
        return None
    return message[0]


# --- The worker and its capped children --------------------------------------------------------

# How often a waiting worker checks that the process that started it still runs.
_PARENT_CHECK: Final = 0.5


def _die_with_parent() -> None:
    """For the capped child only: SIGKILL when the worker's (single) thread exits. Never for
    the worker itself, whose parent thread may be any short-lived thread of the caller's."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


def _serve(conn: Connection, parent: int) -> None:
    """The worker: one capped child per request; it tells the caller how each child ended.

    It exits when the caller's process does (end of file on the socket, or its parent PID
    changing), and after any child that did not end cleanly or left a reply unread."""
    if os.getppid() != parent:
        return
    os.environ.update(_SINGLE_THREADED)
    for name in _PRELOAD:
        __import__(name)
    while _parent_waits(conn, parent):
        try:
            function, args, size, memory = conn.recv()
        except Exception:  # end of file, or a request this worker cannot read
            return
        child = os.fork()
        if child == 0:
            _child(conn, function, args, size, memory)
        ended, clean = _reap(child, parent)
        if ended is None:
            return
        try:
            clean = clean and not conn.poll(0)
            conn.send(("exit", ended, clean))
        except OSError:
            return
        if not clean:
            return


def _parent_waits(conn: Connection, parent: int) -> bool:
    """Wait for a request (or end of file); False once the caller's process has gone."""
    while not conn.poll(_PARENT_CHECK):
        if os.getppid() != parent:
            return False
    return True


def _reap(child: int, parent: int) -> tuple[str | None, bool]:
    """How ``child`` ended and whether cleanly; ``None`` if the caller's process went first,
    in which case the child is killed (by its PID, which is this worker's to reap)."""
    try:
        ended = os.pidfd_open(child)
    except OSError:  # a kernel before 5.3: wait without watching the parent
        ended = -1
    try:
        while ended >= 0 and not select.select([ended], [], [], _PARENT_CHECK)[0]:
            if os.getppid() != parent:
                os.kill(child, signal.SIGKILL)
                os.waitpid(child, 0)
                return None, False
    finally:
        if ended >= 0:
            os.close(ended)
    _, status = os.waitpid(child, 0)
    if os.WIFSIGNALED(status):
        return f"signal {signal.Signals(os.WTERMSIG(status)).name}", False
    code = os.waitstatus_to_exitcode(status)
    return f"exit {code}", code == 0


def _child(
    conn: Connection, function: Callable[..., Any], args: tuple[Any, ...], size: int, memory: int
) -> NoReturn:
    """A capped child: exit 0 only once its last message is sent."""
    code = 1
    try:
        _die_with_parent()
        _capped(conn, function, args, size, memory)
        code = 0
    finally:
        os._exit(code)


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
    remote = Remote(conn, size)
    exceeded = False
    try:
        result = function(remote, *args)
    except MemoryError:
        exceeded = True  # sent once the handler has dropped the frames that held the memory
    conn.send(("memory", remote.replies) if exceeded else ("done", result, remote.replies))


def _data_bytes() -> int:
    """This process's private writable memory, as ``RLIMIT_DATA`` counts it (``VmData``)."""
    with Path("/proc/self/status").open("rb") as status:
        for line in status:
            if line.startswith(b"VmData:"):
                return int(line.split()[1]) * 1024
    raise OSError("no VmData in /proc/self/status")


def _main(fd: int, parent: int) -> None:
    """Run as a script (``-I``, so the import path is empty of the caller's): take the
    caller's import path, then serve as ``neptune_ledger.lake.capped``, the module requests
    name."""
    conn = Connection(fd)
    try:
        path = conn.recv()
    except Exception:
        return
    sys.path[:] = [entry for entry in path if isinstance(entry, str)]
    from neptune_ledger.lake import capped

    capped._serve(conn, parent)


if __name__ == "__main__":
    _main(int(sys.argv[1]), int(sys.argv[2]))
