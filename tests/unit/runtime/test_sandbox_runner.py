"""The sandbox runner: every way a call can end, the limits, and what a confined call cannot do.

Each test runs real work in a real confined child (ADR 0030); nothing is mocked but the two host
checks that a test cannot otherwise make fail.
"""

import contextlib
import ctypes
import errno
import fcntl
import os
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Final

import pytest

from neptune.adapters.contract import ContractError
from neptune.discovery.reader import SourceChangedError
from neptune.runtime import confine
from neptune.runtime.sandbox import (
    DEFAULT_LIMITS,
    Codec,
    Crashed,
    Exceeded,
    InProcess,
    Isolation,
    Limit,
    Limits,
    Raised,
    Returned,
    SandboxError,
    Subprocess,
    decode_raised,
    encode_raised,
    runner,
)

MIB: Final = 1024 * 1024
TEXT: Final = Codec(str, str.encode, bytes.decode)
RAW: Final = Codec(bytes, bytes, bytes)


@pytest.fixture(scope="module")
def box() -> Subprocess:
    return Subprocess(Limits(cpu_seconds=1, wall_seconds=2, memory_bytes=128 * MIB))


def doing(action: Callable[[], object]) -> Callable[[], str]:
    """Work that performs ``action`` and returns what it returned, as text."""

    def work() -> str:
        return str(action())

    return work


def no_children_left() -> bool:
    try:
        os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        return True
    return False


# --- Limits ------------------------------------------------------------------------------------


def test_default_limits() -> None:
    assert Limits(cpu_seconds=60, wall_seconds=120, memory_bytes=2 * 1024 * MIB) == DEFAULT_LIMITS
    assert DEFAULT_LIMITS.to_json() == {
        "cpu_seconds": 60,
        "memory_bytes": 2 * 1024 * MIB,
        "wall_seconds": 120,
    }
    assert DEFAULT_LIMITS.value(Limit.CPU) == 60 and DEFAULT_LIMITS.value(Limit.WALL) == 120
    assert DEFAULT_LIMITS.value(Limit.MEMORY) == DEFAULT_LIMITS.value(Limit.REPLY) == 2 * 1024 * MIB


@pytest.mark.parametrize(
    "limits",
    [
        {"cpu_seconds": 1},
        {"cpu_seconds": 86_400},
        {"wall_seconds": 1},
        {"wall_seconds": 86_400},
        {"memory_bytes": 64 * MIB},
        {"memory_bytes": 1 << 40},
    ],
)
def test_limits_at_their_bounds_are_accepted(limits: dict[str, int]) -> None:
    Limits(**limits)


@pytest.mark.parametrize(
    "limits",
    [
        {"cpu_seconds": 0},
        {"cpu_seconds": -1},
        {"cpu_seconds": 86_401},
        {"cpu_seconds": True},
        {"cpu_seconds": 1.5},
        {"wall_seconds": 0},
        {"wall_seconds": "60"},
        {"memory_bytes": 64 * MIB - 1},
        {"memory_bytes": (1 << 40) + 1},
    ],
)
def test_limits_out_of_bounds_are_refused(limits: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="is an integer in"):
        Limits(**limits)  # type: ignore[arg-type]


# --- Outcomes, in process ----------------------------------------------------------------------


def test_raised_records_the_class_never_the_text() -> None:
    assert Raised.of(RuntimeError("secret path /home/x")) == Raised("RuntimeError")
    assert Raised.of(ContractError("id does not match")) == Raised("ContractError", contract=True)
    assert Raised.of(SourceChangedError("x")) == Raised("SourceChangedError", changed=True)
    odd = type("not an identifier", (Exception,), {})
    assert Raised.of(odd()).error == "Exception"
    with pytest.raises(ValueError, match="class name"):
        Raised("two words")


def test_a_wrong_result_is_a_contract_error_naming_its_type() -> None:
    assert Raised.mistyped(None) == Raised(
        "ContractError", contract=True, returned="builtins.NoneType"
    )
    with pytest.raises(ValueError, match="wrong result"):
        Raised("RuntimeError", returned="builtins.int")  # only a contract error names one
    with pytest.raises(ValueError, match="wrong result"):
        Raised("ContractError", contract=True, returned="not a type name")


def test_the_in_process_runner_returns_or_raises_and_never_isolates() -> None:
    trusted = runner(Isolation.IN_PROCESS)
    assert isinstance(trusted, InProcess)
    assert trusted.describe() == {"isolation": "in_process"}
    assert trusted.call(lambda: "fine", TEXT) == Returned("fine")
    assert trusted.call(doing(lambda: 1 / 0), TEXT) == Raised("ZeroDivisionError")
    assert trusted.call(doing(lambda: os.getpid()), TEXT) == Returned(str(os.getpid()))
    with pytest.raises(KeyboardInterrupt):  # a trusted call's interrupt is the job's
        trusted.call(doing(lambda: signal.raise_signal(signal.SIGINT)), TEXT)


# --- Outcomes, sandboxed -----------------------------------------------------------------------


def test_a_value_crosses_the_boundary_through_its_codec(box: Subprocess) -> None:
    assert box.call(lambda: "fine", TEXT) == Returned("fine")
    child = box.call(doing(lambda: os.getpid()), TEXT)
    assert isinstance(child, Returned) and child.value != str(os.getpid())
    assert box.call(lambda: b"", RAW) == Returned(b"")


def test_what_the_call_raises_comes_back_as_its_class(box: Subprocess) -> None:
    assert box.call(doing(lambda: 1 / 0), TEXT) == Raised("ZeroDivisionError")

    def contract() -> str:
        raise ContractError("ingest returned a NoneType")

    assert box.call(contract, TEXT) == Raised("ContractError", contract=True)

    def changed() -> str:
        raise SourceChangedError("chunk 3 changed")

    assert box.call(changed, TEXT) == Raised("SourceChangedError", changed=True)

    def leave() -> str:
        raise SystemExit(0)

    assert box.call(leave, TEXT) == Raised("SystemExit")


def test_a_result_of_the_wrong_type_is_caught_in_either_isolation(box: Subprocess) -> None:
    wrong = Raised("ContractError", contract=True, returned="builtins.int")
    assert box.call(lambda: 42, TEXT) == wrong
    assert InProcess().call(lambda: 42, TEXT) == wrong
    assert box.call(lambda: None, TEXT) == Raised.mistyped(None)


def test_a_process_that_dies_is_a_crash_with_its_signal_or_status(box: Subprocess) -> None:
    assert box.call(doing(lambda: ctypes.string_at(0)), TEXT) == Crashed(signal="SIGSEGV")
    assert box.call(doing(lambda: os.abort()), TEXT) == Crashed(signal="SIGABRT")
    die = box.call(doing(lambda: os.kill(os.getpid(), signal.SIGKILL)), TEXT)
    assert die == Crashed(signal="SIGKILL")
    assert box.call(doing(lambda: os._exit(3)), TEXT) == Crashed(exit_status=3)
    assert box.call(doing(lambda: os._exit(0)), TEXT) == Crashed(exit_status=0)  # no reply
    assert no_children_left()  # every child was reaped


def test_a_reply_that_does_not_decode_is_a_crash(box: Subprocess) -> None:
    def refuse(data: bytes) -> str:
        raise ValueError("not a valid reply")

    assert box.call(lambda: "fine", Codec(str, str.encode, refuse)) == Crashed()
    assert Crashed().cause() == {"reply": "malformed"}


def test_a_hang_is_stopped_at_the_wall_limit(box: Subprocess) -> None:
    started = time.monotonic()
    assert box.call(doing(lambda: time.sleep(60)), TEXT) == Exceeded(Limit.WALL, 2)
    assert 2 <= time.monotonic() - started < 10
    assert no_children_left()


def test_a_spin_is_stopped_at_the_cpu_limit() -> None:
    spinner = Subprocess(Limits(cpu_seconds=1, wall_seconds=30, memory_bytes=128 * MIB))

    def spin() -> str:
        while True:
            pass

    started = time.monotonic()
    assert spinner.call(spin, TEXT) == Exceeded(Limit.CPU, 1)
    assert time.monotonic() - started < 10


def test_allocating_without_bound_is_stopped_at_the_memory_limit(box: Subprocess) -> None:
    def hog() -> str:
        held = []
        while True:
            held.append(bytearray(8 * MIB))

    assert box.call(hog, TEXT) == Exceeded(Limit.MEMORY, 128 * MIB)
    assert box.call(lambda: "x" * (256 * MIB), TEXT) == Exceeded(Limit.MEMORY, 128 * MIB)
    assert box.call(lambda: "x" * (8 * MIB), TEXT) == Returned("x" * (8 * MIB))  # within it


def test_a_reply_larger_than_the_memory_limit_is_refused() -> None:
    small = Subprocess(Limits(memory_bytes=64 * MIB))
    inherited = b"x" * (80 * MIB)  # mapped before the fork: the child adds nothing to send it
    assert small.call(lambda: inherited, RAW) == Exceeded(Limit.REPLY, 64 * MIB)
    assert small.call(lambda: inherited[: 60 * MIB], RAW) == Returned(inherited[: 60 * MIB])
    # One byte over: the excess may still sit in the pipe when the child exits, and is the limit
    # every time, never a decoded reply.
    over = inherited[: 64 * MIB + 1]
    assert {small.call(lambda: over, RAW) for _ in range(5)} == {Exceeded(Limit.REPLY, 64 * MIB)}
    exact = inherited[: 64 * MIB]
    assert small.call(lambda: exact, RAW) == Returned(exact)


def test_outcomes_are_deterministic(box: Subprocess) -> None:
    def calls() -> list[object]:
        return [
            box.call(lambda: "fine", TEXT),
            box.call(doing(lambda: 1 / 0), TEXT),
            box.call(doing(lambda: ctypes.string_at(0)), TEXT),
            box.call(doing(lambda: os._exit(5)), TEXT),
        ]

    assert calls() == calls()


def test_causes_name_only_classes_signals_statuses_and_limits() -> None:
    assert Raised("OSError").cause() == {"error": "OSError"}
    assert Crashed(signal="SIGSEGV").cause() == {"signal": "SIGSEGV"}
    assert Crashed(exit_status=7).cause() == {"exit_status": 7}
    assert Exceeded(Limit.WALL, 2).cause() == {"limit": "wall_seconds", "value": 2}


# --- What a confined call cannot do ------------------------------------------------------------


def test_no_network(box: Subprocess) -> None:
    for family in (socket.AF_INET, socket.AF_INET6, socket.AF_UNIX):
        outcome = box.call(doing(partial(socket.socket, family)), TEXT)
        assert outcome == Raised("PermissionError")


def test_no_new_process_or_program(box: Subprocess) -> None:
    assert box.call(doing(lambda: os.fork()), TEXT) == Raised("PermissionError")
    assert box.call(doing(lambda: subprocess.run(["true"])), TEXT) == Raised("PermissionError")
    assert box.call(doing(lambda: os.execv("/bin/true", ["true"])), TEXT) == Raised(
        "PermissionError"
    )


def test_threads_still_work(box: Subprocess) -> None:
    def threaded() -> str:
        out: list[str] = []
        worker = threading.Thread(target=lambda: out.append("from a thread"))
        worker.start()
        worker.join()
        return out[0]

    assert box.call(threaded, TEXT) == Returned("from a thread")


def test_no_signal_to_another_process(box: Subprocess) -> None:
    parent = os.getpid()
    assert box.call(doing(lambda: os.kill(parent, 0)), TEXT) == Raised("PermissionError")
    assert box.call(doing(lambda: os.kill(0, 0)), TEXT) == Raised("PermissionError")  # its group
    assert box.call(doing(lambda: os.kill(os.getpid(), 0)), TEXT) == Returned("None")  # itself


def test_no_signal_through_async_io_descriptor_ownership(box: Subprocess) -> None:
    """The kernel also delivers a signal through a descriptor's owner (``fcntl`` F_SETOWN, the
    ``ioctl`` FIOASYNC/FIOSETOWN requests), which only Landlock ABI 6 scopes. Seccomp refuses it
    on every ABI, and a refusal is a raise the job survives, not a dead job."""

    def set_owner() -> str:
        read_fd, write_fd = os.pipe()  # a pipe, not a socket: socket() itself is denied
        try:
            return str(fcntl.fcntl(write_fd, fcntl.F_SETOWN, os.getppid()))
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def set_async() -> str:
        read_fd, write_fd = os.pipe()
        try:
            return str(fcntl.ioctl(write_fd, termios.FIOASYNC, struct.pack("i", 1)))
        finally:
            os.close(read_fd)
            os.close(write_fd)

    assert box.call(set_owner, TEXT) == Raised("PermissionError")
    assert box.call(set_async, TEXT) == Raised("PermissionError")
    # The job is unharmed: an ordinary call right after still returns.
    assert box.call(lambda: "alive", TEXT) == Returned("alive")


def test_a_child_cannot_clear_its_parent_death_signal(box: Subprocess) -> None:
    """A hostile child must not clear ``PR_SET_PDEATHSIG`` (it would outlive a killed job) or make
    itself dumpable again (a core dump holding source data): seccomp refuses both options."""
    libc = ctypes.CDLL(None, use_errno=True)

    def clear_pdeathsig() -> str:
        ctypes.set_errno(0)
        result = libc.prctl(1, 0, 0, 0, 0)  # PR_SET_PDEATHSIG, no signal
        return f"{result} {ctypes.get_errno()}"

    def make_dumpable() -> str:
        ctypes.set_errno(0)
        result = libc.prctl(4, 1, 0, 0, 0)  # PR_SET_DUMPABLE, dumpable
        return f"{result} {ctypes.get_errno()}"

    for work in (clear_pdeathsig, make_dumpable):
        outcome = box.call(work, TEXT)
        assert isinstance(outcome, Returned)
        result, code = outcome.value.split()
        assert result == "-1" and int(code) == errno.EPERM


def test_nothing_is_written_to_any_file(box: Subprocess, tmp_path: Path) -> None:
    target = tmp_path / "escaped"
    assert box.call(doing(lambda: target.write_bytes(b"escaped")), TEXT) == Raised(
        "PermissionError" if box.landlock else "OSError"
    )
    assert not target.exists() or target.read_bytes() == b""  # RLIMIT_FSIZE: never a byte


@pytest.mark.skipif(not confine.landlock_abi(), reason="this kernel has no Landlock")
def test_with_landlock_nothing_is_created_changed_or_removed(
    box: Subprocess, tmp_path: Path
) -> None:
    kept = tmp_path / "kept"
    kept.write_bytes(b"source bytes")
    denied = Raised("PermissionError")
    assert box.call(doing(lambda: (tmp_path / "new").write_bytes(b"")), TEXT) == denied
    assert box.call(doing(lambda: (tmp_path / "dir").mkdir()), TEXT) == denied
    assert box.call(doing(lambda: kept.unlink()), TEXT) == denied
    assert box.call(doing(lambda: os.truncate(kept, 0)), TEXT) == denied
    assert box.call(doing(lambda: kept.rename(tmp_path / "moved")), TEXT) == denied
    assert box.call(lambda: kept.read_bytes().decode(), TEXT) == Returned("source bytes")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["kept"]
    assert kept.read_bytes() == b"source bytes"


def test_only_kept_descriptors_stay_open(box: Subprocess, tmp_path: Path) -> None:
    (tmp_path / "kept").write_bytes(b"kept")
    (tmp_path / "other").write_bytes(b"other")
    with (tmp_path / "kept").open("rb") as kept, (tmp_path / "other").open("rb") as other:
        read = box.call(lambda: os.pread(kept.fileno(), 4, 0), RAW, keep=(kept.fileno(),))
        assert read == Returned(b"kept")
        closed = box.call(lambda: os.pread(other.fileno(), 5, 0), RAW, keep=(kept.fileno(),))
        assert closed == Raised("OSError")  # EBADF: never inherited
    with pytest.raises(ValueError, match="descriptor"):
        box.call(lambda: "x", TEXT, keep=(-1,))


def test_what_the_child_prints_never_reaches_the_jobs_output(
    box: Subprocess, capfd: pytest.CaptureFixture[str]
) -> None:
    def chatty() -> str:
        print("leaked source text")  # noqa: T201 - the point of the test
        os.write(2, b"leaked to stderr\n")
        return "done"

    assert box.call(chatty, TEXT) == Returned("done")
    out, err = capfd.readouterr()
    assert "leaked" not in out and "leaked" not in err


def test_a_hostile_call_cannot_make_the_sandbox_look_broken(box: Subprocess) -> None:
    def forge() -> str:
        for entry in Path("/proc/self/fd").iterdir():
            with contextlib.suppress(OSError):  # the read-only and closed ones
                os.write(int(entry.name), b"Useccomp")
        return "forged"

    assert box.call(forge, TEXT) == Crashed()  # a garbled reply, not a SandboxError


def test_the_child_dies_when_the_job_does(tmp_path: Path) -> None:
    script = tmp_path / "job.py"
    script.write_text(
        "import time\n"
        "from neptune.runtime.sandbox import Codec, Limits, Subprocess\n"
        "box = Subprocess(Limits(wall_seconds=600))\n"
        "box.call(lambda: str(time.sleep(600)), Codec(str, str.encode, bytes.decode))\n"
    )
    job = subprocess.Popen([sys.executable, str(script)])
    try:
        children: list[int] = []
        deadline = time.monotonic() + 30
        while not children and time.monotonic() < deadline:
            time.sleep(0.05)
            tasks = Path(f"/proc/{job.pid}/task")
            for task in tasks.iterdir() if tasks.exists() else ():
                children += [int(p) for p in (task / "children").read_text().split()]
        assert children, "the job never started its sandboxed call"
    finally:
        job.kill()
        job.wait()
    deadline = time.monotonic() + 10
    while Path(f"/proc/{children[0]}").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not Path(f"/proc/{children[0]}").exists()  # killed with its parent, then reaped


# --- The host ----------------------------------------------------------------------------------


def test_the_sandbox_describes_its_limits_and_landlock(box: Subprocess) -> None:
    assert box.describe() == {
        "isolation": "subprocess",
        "landlock": confine.landlock_abi(),
        "limits": {"cpu_seconds": 1, "memory_bytes": 128 * MIB, "wall_seconds": 2},
    }


def test_a_host_without_the_controls_cannot_build_a_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lacking() -> confine.Host:
        raise confine.ConfineError("platform", "the sandbox needs Linux, not plan9")

    monkeypatch.setattr(confine, "host", lacking)
    with pytest.raises(SandboxError, match="plan9"):
        Subprocess()


def test_a_control_that_fails_in_the_child_is_the_hosts_fault(
    box: Subprocess, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*args: object) -> None:
        raise confine.ConfineError("seccomp", "EINVAL")

    monkeypatch.setattr(confine, "confine", refused)  # the child inherits it at fork
    with pytest.raises(SandboxError, match="seccomp failed"):
        box.call(lambda: "never runs", TEXT)


def test_a_raised_reply_is_decoded_strictly() -> None:
    for raised in (
        Raised("OSError"),
        Raised("SourceChangedError", changed=True),
        Raised("ContractError", contract=True, returned="builtins.NoneType"),
    ):
        assert decode_raised(encode_raised(raised)) == raised
    for data in (
        b"",
        b"[]",
        b'{"changed":false,"contract":false,"error":"E"}',
        b'{"changed":false,"contract":false,"error":"E","returned":null,"extra":1}',
        b'{"changed":0,"contract":false,"error":"E","returned":null}',
        b'{"changed":false,"contract":false,"error":"two words","returned":null}',
        b'{"changed":false,"contract":false,"error":"E","returned":"builtins.int"}',
        b'{"changed":false,"contract":true,"error":"E","returned":"x y"}',
        b'{"changed":false,"contract":"yes","error":"E","returned":null}',
    ):
        with pytest.raises(ValueError):
            decode_raised(data)
