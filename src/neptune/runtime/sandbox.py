"""Running an adapter's call in a confined child process, or in this one when asked (ADR 0030).

The job hands a runner one unit of adapter work at a time (a probe, a plan, one chunk's
``ingest``) and gets back one of four outcomes, never an exception:

- ``Returned``: the call's value;
- ``Raised``: the call raised; the exception's class, and whether it was a contract violation, a
  source that changed, or an operating-system error;
- ``Crashed``: the process died without a reply: killed by a signal, an exit, or a reply that
  does not decode;
- ``Exceeded``: a limit stopped it: CPU seconds, wall seconds, memory, or the reply's size.

``Subprocess`` is the default. It forks once per call, so the child runs the adapter object the
job built with nothing to import or pickle, confines it (``neptune.runtime.confine``: limits,
descriptors, Landlock, seccomp) before any adapter code runs, and reads its reply from a pipe as
JSON (``neptune.runtime.wire``). Nothing the child does reaches the workspace: the job commits
what the parent decoded and checked, so a killed child leaves no partial chunk anywhere.
``InProcess`` calls the adapter directly, for tests and trusted adapters, and only when chosen.
"""

import contextlib
import json
import math
import os
import re
import select
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Generic, NoReturn, Protocol, TypeAlias, TypeVar

from neptune.adapters.contract import ContractError
from neptune.discovery.reader import SourceChangedError
from neptune.model.jsonvalue import JsonObject, JsonValue
from neptune.runtime import confine

T = TypeVar("T")

_MIB: Final = 1024 * 1024
_MAX_SECONDS: Final = 24 * 60 * 60
_MIN_MEMORY: Final = 64 * _MIB  # below this the interpreter itself cannot run a call
_MAX_MEMORY: Final = 1 << 40
_ERROR_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")


class Isolation(StrEnum):
    """Where adapter code runs."""

    SUBPROCESS = "subprocess"  # a confined child process per call: the default
    IN_PROCESS = "in_process"  # this process, unconfined: tests and trusted adapters, by choice


class Limit(StrEnum):
    """What can stop a sandboxed call, by the name of the setting that bounds it."""

    CPU = "cpu_seconds"
    WALL = "wall_seconds"
    MEMORY = "memory_bytes"
    REPLY = "reply_bytes"  # the reply may not exceed what the call was allowed to hold


@dataclass(frozen=True)
class Limits:
    """The bounds of one sandboxed call (one probe, one plan, one chunk's ``ingest``).

    ``cpu_seconds`` is CPU time; ``wall_seconds`` elapsed time; ``memory_bytes`` the address
    space the call may add to what the process held when it forked. A reply larger than
    ``memory_bytes`` is refused too.
    """

    cpu_seconds: int = 60
    wall_seconds: int = 120
    memory_bytes: int = 2 * 1024 * _MIB

    def __post_init__(self) -> None:
        for name, value, low, high in (
            ("cpu_seconds", self.cpu_seconds, 1, _MAX_SECONDS),
            ("wall_seconds", self.wall_seconds, 1, _MAX_SECONDS),
            ("memory_bytes", self.memory_bytes, _MIN_MEMORY, _MAX_MEMORY),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} is an integer in [{low}, {high}], got {value!r}")

    def value(self, limit: Limit) -> int:
        if limit is Limit.CPU:
            return self.cpu_seconds
        if limit is Limit.WALL:
            return self.wall_seconds
        return self.memory_bytes

    def to_json(self) -> JsonObject:
        return {
            "cpu_seconds": self.cpu_seconds,
            "memory_bytes": self.memory_bytes,
            "wall_seconds": self.wall_seconds,
        }


DEFAULT_LIMITS: Final = Limits()


# --- Outcomes ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Returned(Generic[T]):
    value: T


@dataclass(frozen=True)
class Raised:
    """The call raised. ``error`` is the exception's class name, never its text.

    ``contract`` marks a ``ContractError`` (a bug: never retried), with its message as
    ``problem``; ``changed`` a ``SourceChangedError`` (the source's bytes are not the ones
    fingerprinted: never retried, reported as the source's, not the adapter's).
    """

    error: str
    contract: bool = False
    changed: bool = False
    problem: str | None = None

    def __post_init__(self) -> None:
        if not _ERROR_NAME.fullmatch(self.error):
            raise ValueError(f"not an exception class name: {self.error!r}")

    @classmethod
    def of(cls, exc: BaseException) -> "Raised":
        name = type(exc).__name__
        contract = isinstance(exc, ContractError)
        return cls(
            error=name if _ERROR_NAME.fullmatch(name) else "Exception",
            contract=contract,
            changed=isinstance(exc, SourceChangedError),
            problem=str(exc) if contract else None,
        )

    def cause(self) -> dict[str, JsonValue]:
        return {"error": self.error}


@dataclass(frozen=True)
class Crashed:
    """The process died without a valid reply: by ``signal``, with ``exit_status``, or neither
    (its reply did not decode)."""

    signal: str | None = None
    exit_status: int | None = None

    def cause(self) -> dict[str, JsonValue]:
        if self.signal is not None:
            return {"signal": self.signal}
        if self.exit_status is not None:
            return {"exit_status": self.exit_status}
        return {"reply": "malformed"}


@dataclass(frozen=True)
class Exceeded:
    """A limit stopped the call: which one, and its value."""

    limit: Limit
    value: int

    def cause(self) -> dict[str, JsonValue]:
        return {"limit": str(self.limit), "value": self.value}


Outcome: TypeAlias = Returned[T] | Raised | Crashed | Exceeded


class SandboxError(Exception):
    """The sandbox cannot run on this host. A job fails with it: no adapter code has run."""


@dataclass(frozen=True)
class Codec(Generic[T]):
    """How a call's value crosses the process boundary. ``decode`` must refuse what is not a
    valid encoding: its input comes from a process that read hostile bytes."""

    encode: Callable[[T], bytes]
    decode: Callable[[bytes], T]


class Runner(Protocol):
    isolation: Isolation

    def call(
        self, work: Callable[[], T], codec: Codec[T], keep: tuple[int, ...] = ()
    ) -> Returned[T] | Raised | Crashed | Exceeded:
        """Run ``work``; ``keep`` lists the descriptors it reads (a source's)."""
        ...

    def describe(self) -> JsonObject:
        """What isolates the calls: for the job's ``sandbox_ready`` event."""
        ...


class InProcess:
    """Adapter code runs here, unconfined and unlimited. Chosen explicitly, never a default."""

    isolation = Isolation.IN_PROCESS

    def call(
        self, work: Callable[[], T], codec: Codec[T], keep: tuple[int, ...] = ()
    ) -> Returned[T] | Raised | Crashed | Exceeded:
        try:
            return Returned(work())
        except Exception as exc:
            return Raised.of(exc)

    def describe(self) -> JsonObject:
        return {"isolation": str(self.isolation)}


# --- The reply -----------------------------------------------------------------------------------

# The child writes one status byte once it is confined, before any adapter code runs, so a
# hostile adapter can never claim the sandbox failed to start; then the call's reply.
_CONFINED: Final = b"C"
_UNCONFINED: Final = b"U"
_RETURNED: Final = b"R"
_RAISED: Final = b"E"
_OUT_OF_MEMORY: Final = b"M"
_UNREPORTED: Final = 70  # the child's exit status when it could not even write its reply


def encode_raised(raised: Raised) -> bytes:
    return json.dumps(
        {
            "changed": raised.changed,
            "contract": raised.contract,
            "error": raised.error,
            "problem": raised.problem,
        },
        separators=(",", ":"),
    ).encode("ascii")


def decode_raised(data: bytes) -> Raised:
    """A ``Raised`` reply, strictly: exactly its four fields, each of its type."""
    value = json.loads(data)
    if not isinstance(value, dict) or value.keys() != {"changed", "contract", "error", "problem"}:
        raise ValueError("a raised reply is exactly {changed, contract, error, problem}")
    error, contract = value["error"], value["contract"]
    changed, problem = value["changed"], value["problem"]
    if not isinstance(error, str) or not isinstance(contract, bool):
        raise ValueError("a raised reply's error is text and contract a boolean")
    if not isinstance(changed, bool) or not (problem is None or isinstance(problem, str)):
        raise ValueError("a raised reply's changed is a boolean and problem text or null")
    if problem is not None and not contract:
        raise ValueError("only a contract violation has a problem")
    return Raised(error, contract, changed, problem)


def _send(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _read(fd: int) -> bytes | None:
    """What the pipe holds now: ``b""`` at its end, ``None`` if nothing yet."""
    try:
        return os.read(fd, _MIB)
    except BlockingIOError:
        return None


def _kill(pidfd: int) -> None:
    with contextlib.suppress(ProcessLookupError):  # it exited first
        signal.pidfd_send_signal(pidfd, signal.SIGKILL)


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"SIG{signum}"


def _nothing(data: bytes) -> None:
    if data:
        raise ValueError("expected an empty reply")


_NOTHING: Final = Codec[None](lambda _: b"", _nothing)


class Subprocess:
    """Each call in a fresh child process, confined and bounded by ``limits`` (Linux).

    Building one checks the host and runs one empty call through every control, so a host that
    cannot confine fails here, before any source is read, with a ``SandboxError``.
    """

    isolation = Isolation.SUBPROCESS

    def __init__(self, limits: Limits = DEFAULT_LIMITS) -> None:
        if not isinstance(limits, Limits):
            raise TypeError(f"limits must be Limits, got {limits!r}")
        try:
            self._host = confine.host()
        except confine.ConfineError as exc:
            raise SandboxError(f"this host cannot run the sandbox: {exc}") from exc
        self.limits = limits
        if not isinstance(self.call(lambda: None, _NOTHING), Returned):
            raise SandboxError("this host cannot run the sandbox: an empty call failed")

    @property
    def landlock(self) -> int:
        """The Landlock ABI the children apply; 0 where the kernel has none."""
        return self._host.landlock

    def describe(self) -> JsonObject:
        return {
            "isolation": str(self.isolation),
            "landlock": self._host.landlock,
            "limits": self.limits.to_json(),
        }

    def call(
        self, work: Callable[[], T], codec: Codec[T], keep: tuple[int, ...] = ()
    ) -> Returned[T] | Raised | Crashed | Exceeded:
        for fd in keep:
            if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
                raise ValueError(f"not a descriptor: {fd!r}")
        reply, write_end = os.pipe()
        parent = os.getpid()
        try:
            pid = os.fork()
        except OSError as exc:
            os.close(reply)
            os.close(write_end)
            raise SandboxError(f"cannot start a sandboxed call: {exc}") from exc
        if pid == 0:  # pragma: no cover - the child's coverage is not collected
            self._child(work, codec, frozenset(keep) | {write_end}, write_end, parent)
        os.close(write_end)
        try:
            return self._await(pid, reply, codec)
        finally:
            os.close(reply)

    def _child(
        self,
        work: Callable[[], T],
        codec: Codec[T],
        keep: frozenset[int],
        reply: int,
        parent: int,
    ) -> NoReturn:  # pragma: no cover - runs in the child, whose coverage is not collected
        try:
            try:
                confine.confine(
                    self._host, keep, parent, self.limits.cpu_seconds, self.limits.memory_bytes
                )
            except confine.ConfineError as exc:
                _send(reply, _UNCONFINED + exc.control.encode("ascii", "replace"))
                os._exit(0)
            _send(reply, _CONFINED)
            # The tag and the payload are sent apart, so the reply is never copied to prefix it.
            tag, payload = _OUT_OF_MEMORY, b""  # held before the call: needs no allocation
            try:
                try:
                    payload = codec.encode(work())
                    tag = _RETURNED
                except MemoryError:
                    pass
                except BaseException as exc:
                    payload = encode_raised(Raised.of(exc))
                    tag = _RAISED
            except MemoryError:
                tag, payload = _OUT_OF_MEMORY, b""
            _send(reply, tag)
            _send(reply, payload)
            os._exit(0)
        except BaseException:
            os._exit(_UNREPORTED)

    def _await(self, pid: int, reply: int, codec: Codec[T]) -> Outcome[T]:
        """Read the reply until the child exits or a limit is hit; reap it; classify."""
        pidfd = os.pidfd_open(pid)
        os.set_blocking(reply, False)
        cap = self.limits.memory_bytes + 2  # the status byte and the reply's tag
        pieces: list[bytes] = []
        size = 0
        killed: Limit | None = None
        reaped = False
        poller = select.poll()
        poller.register(reply, select.POLLIN)
        poller.register(pidfd, select.POLLIN)
        deadline = time.monotonic() + self.limits.wall_seconds
        try:
            exited = False
            while not exited and killed is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    killed = Limit.WALL
                    break
                for fd, _ in poller.poll(math.ceil(remaining * 1000)):
                    if fd == pidfd:
                        exited = True
                        continue
                    piece = _read(reply)
                    if piece == b"":
                        poller.unregister(reply)
                    elif piece:
                        pieces.append(piece)
                        size += len(piece)
                        if size > cap:
                            killed = Limit.REPLY
            while killed is None and (piece := _read(reply)):  # what is left after the exit
                pieces.append(piece)
                size += len(piece)
                if size > cap:
                    killed = Limit.REPLY
            if killed is not None:
                _kill(pidfd)
            _, status, usage = os.wait4(pid, 0)
            reaped = True
        finally:
            if not reaped:
                _kill(pidfd)
                os.waitpid(pid, 0)
            os.close(pidfd)
        data = b"".join(pieces)
        if data[:1] == _UNCONFINED:  # written before any adapter code ran: the host's fault
            control = data[1:].decode("ascii", "replace")
            raise SandboxError(f"this host cannot run the sandbox: {control} failed")
        return self._classify(status, usage.ru_utime + usage.ru_stime, killed, data, codec)

    def _classify(
        self, status: int, cpu: float, killed: Limit | None, data: bytes, codec: Codec[T]
    ) -> Outcome[T]:
        limits = self.limits
        if os.WIFSIGNALED(status):
            signum = os.WTERMSIG(status)
            if signum == signal.SIGKILL and killed is not None:
                return Exceeded(killed, limits.value(killed))
            if signum == signal.SIGXCPU or (signum == signal.SIGKILL and cpu >= limits.cpu_seconds):
                return Exceeded(Limit.CPU, limits.cpu_seconds)
            return Crashed(signal=_signal_name(signum))
        code = os.waitstatus_to_exitcode(status)
        if code != 0 or data[:1] != _CONFINED or len(data) < 2:
            return Crashed(exit_status=code)
        tag, payload = data[1:2], data[2:]
        try:
            if tag == _RETURNED:
                return Returned(codec.decode(payload))
            if tag == _RAISED:
                return decode_raised(payload)
            if tag == _OUT_OF_MEMORY and not payload:
                return Exceeded(Limit.MEMORY, limits.memory_bytes)
        except Exception:  # any failure to decode: the reply is not one a sound child writes
            pass
        return Crashed()


def runner(isolation: Isolation, limits: Limits = DEFAULT_LIMITS) -> Runner:
    """The runner for ``isolation``; ``SandboxError`` if this host cannot confine a call."""
    if isolation is Isolation.IN_PROCESS:
        return InProcess()
    return Subprocess(limits)
