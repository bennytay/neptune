"""The sandbox runner: every way a call can end, the limits, and what a confined call cannot do.

Each test runs real work in a real confined child (ADR 0030); nothing is mocked but the two host
checks that a test cannot otherwise make fail.
"""

import contextlib
import ctypes
import errno
import fcntl
import os
import pty
import select
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
from neptune.runtime import confine, wire
from neptune.runtime.sandbox import (
    DEFAULT_LIMITS,
    REQUIRED_LANDLOCK_ABI,
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
    landlock_guarantees_lost,
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
    assert (
        Limits(cpu_seconds=60, wall_seconds=120, memory_bytes=2 * 1024 * MIB, reply_bytes=64 * MIB)
        == DEFAULT_LIMITS
    )
    assert DEFAULT_LIMITS.to_json() == {
        "cpu_seconds": 60,
        "memory_bytes": 2 * 1024 * MIB,
        "reply_bytes": 64 * MIB,
        "wall_seconds": 120,
    }
    assert DEFAULT_LIMITS.value(Limit.CPU) == 60 and DEFAULT_LIMITS.value(Limit.WALL) == 120
    assert DEFAULT_LIMITS.value(Limit.MEMORY) == 2 * 1024 * MIB  # the address space
    assert DEFAULT_LIMITS.value(Limit.REPLY) == 64 * MIB  # far below it: a separate cap


@pytest.mark.parametrize(
    "limits",
    [
        {"cpu_seconds": 1},
        {"cpu_seconds": 86_400},
        {"wall_seconds": 1},
        {"wall_seconds": 86_400},
        {"memory_bytes": 64 * MIB},
        {"memory_bytes": 1 << 40},
        {"reply_bytes": 64 * 1024},
        {"reply_bytes": 1 << 40},
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
        {"reply_bytes": 64 * 1024 - 1},
        {"reply_bytes": (1 << 40) + 1},
        {"reply_bytes": True},
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


def test_the_reply_cap_is_separate_from_and_far_below_the_memory_limit() -> None:
    # A small reply cap holds, even when the child has plenty of address space: one hostile call
    # that emits a giant reply cannot exhaust the job as the parent copies and decodes it.
    box = Subprocess(Limits(memory_bytes=256 * MIB, reply_bytes=2 * MIB))
    inherited = b"x" * (8 * MIB)  # mapped before the fork: well within the child's 256 MiB
    assert box.call(lambda: inherited, RAW) == Exceeded(Limit.REPLY, 2 * MIB)
    assert box.call(lambda: inherited[: 1 * MIB], RAW) == Returned(inherited[: 1 * MIB])
    # One byte over: the excess may still sit in the pipe when the child exits, and is the limit
    # every time, never a decoded reply.
    over = inherited[: 2 * MIB + 1]
    assert {box.call(lambda: over, RAW) for _ in range(5)} == {Exceeded(Limit.REPLY, 2 * MIB)}
    exact = inherited[: 2 * MIB]
    assert box.call(lambda: exact, RAW) == Returned(exact)


def test_a_reply_that_packs_the_byte_cap_with_values_is_refused_not_decoded() -> None:
    # Under the byte cap but far over the node cap: a flat array of bare zeros, and a deeply
    # nested one. Either would build millions of Python objects; the decode refuses both as the
    # reply's own limit, so the chunk fails and the job carries on.
    box = Subprocess(Limits(memory_bytes=512 * MIB, reply_bytes=128 * MIB))
    elements = b"[" + b"0," * (wire._MAX_REPLY_NODES + 8) + b"0]"
    nested = b"[" * (wire._MAX_REPLY_NODES + 8) + b"]" * (wire._MAX_REPLY_NODES + 8)
    assert len(elements) < 128 * MIB and len(nested) < 128 * MIB  # both within the byte cap
    flat_codec = Codec(object, lambda _: elements, wire._loads)
    deep_codec = Codec(object, lambda _: nested, wire._loads)
    assert box.call(lambda: [], flat_codec) == Exceeded(Limit.REPLY, 128 * MIB)
    assert box.call(lambda: [], deep_codec) == Exceeded(Limit.REPLY, 128 * MIB)


def rebuild_without_end(data: bytes) -> object:
    """A decode that parses, then recurses as a rebuild of deeply nested JSON would."""

    def descend(value: object) -> object:
        return descend([value])

    return descend(wire._loads(data))


def test_a_reply_nested_too_deep_is_the_reply_limit_not_a_crash(box: Subprocess) -> None:
    # Far under the node cap (200 KB), yet nested past the parser's recursion guard: the reply's
    # limit, never a crash, which the job would retry only to meet the same bytes again. The same
    # holds where the nesting strikes while the model rebuilds what parsed.
    deep = b"[" * 100_000 + b"]" * 100_000
    assert box.call(lambda: [], Codec(object, lambda _: deep, wire._loads)) == Exceeded(
        Limit.REPLY, box.limits.reply_bytes
    )
    rebuilt = box.call(lambda: [], Codec(object, lambda _: b"[]", rebuild_without_end))
    assert rebuilt == Exceeded(Limit.REPLY, box.limits.reply_bytes)
    assert box.call(lambda: "fine", TEXT) == Returned("fine")  # the next call is unharmed


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


def async_on_terminal(path: str) -> str:
    """Sandboxed work: open the job's terminal read-only, as Landlock allows, and try to turn
    ``O_ASYNC`` on with ``F_SETFL``; then wait for a keystroke, which would raise SIGIO in the
    terminal's foreground process group, the job's, had the flag been set. Plain ``F_SETFL``
    (``O_NONBLOCK``, via ``os.set_blocking``) is used on the way."""
    fd = os.open(path, os.O_RDONLY)
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_ASYNC)
            refused = "set"
        except PermissionError:
            refused = "PermissionError"
        os.set_blocking(fd, False)
        nonblocking = not os.get_blocking(fd)
        with contextlib.suppress(BlockingIOError):  # drain what was typed before
            while os.read(fd, 64):
                pass
        os.set_blocking(fd, True)
        os.read(fd, 64)  # a keystroke typed after the attempt
        return f"{refused} nonblocking={nonblocking}"
    finally:
        os.close(fd)


@pytest.mark.filterwarnings("ignore:This process .* use of forkpty:DeprecationWarning")
def test_no_signal_through_o_async_on_the_jobs_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """``F_SETFL`` with ``O_ASYNC`` on a terminal makes the kernel aim SIGIO at the terminal's
    foreground process group, the job's, with no ``F_SETOWN``; ``setsid`` cuts only ``/dev/tty``
    and the job's ``/dev/pts/N`` stays readable. Seccomp refuses the flag on every ABI. The job
    here is a session leader in its terminal's foreground, as in a shell, and runs at Landlock ABI
    4 (Ubuntu 24.04), or the host's own if lower, so ABI 6 signal scoping cannot hide a gap; the
    default action of SIGIO would end it at the next keystroke."""
    real = confine.host()
    monkeypatch.setattr(confine, "host", lambda: confine.Host(real.arch, min(real.landlock, 4)))
    pid, master = pty.fork()
    if pid == 0:  # the job, its controlling terminal the pty, in that terminal's foreground
        code = 3
        try:
            box = Subprocess(Limits(cpu_seconds=5, wall_seconds=20, memory_bytes=128 * MIB))
            outcome = box.call(partial(async_on_terminal, os.ttyname(0)), TEXT)
            os.write(1, f"\noutcome: {outcome!r}\n".encode())
            code = 0 if outcome == Returned("PermissionError nonblocking=True") else 4
        finally:
            os._exit(code)
    output = bytearray()
    status: int | None = None
    deadline = time.monotonic() + 30
    try:
        while status is None and time.monotonic() < deadline:
            os.write(master, b"k\n")  # a keystroke, again and again, while the call runs
            if select.select([master], [], [], 0.05)[0]:
                with contextlib.suppress(OSError):  # EIO once the job has closed the terminal
                    output += os.read(master, 4096)
            done, waited = os.waitpid(pid, os.WNOHANG)
            status = waited if done else None
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(master)
    assert status is not None, "the job did not finish"
    if os.WIFSIGNALED(status):
        pytest.fail(f"the job was killed by {signal.Signals(os.WTERMSIG(status)).name}")
    assert os.waitstatus_to_exitcode(status) == 0, bytes(output)


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


def test_a_confined_call_cannot_change_a_files_metadata(box: Subprocess, tmp_path: Path) -> None:
    """Landlock covers no metadata syscall, so seccomp refuses them on every ABI: a parser cannot
    chmod the source unreadable, retime it to defeat change detection, or set an xattr."""
    source = tmp_path / "source"
    source.write_bytes(b"source bytes")
    before = source.stat()
    chmod = box.call(doing(lambda: os.chmod(source, 0o600)), TEXT)  # noqa: PTH101 - the attack
    assert chmod == Raised("PermissionError")
    assert box.call(doing(lambda: os.utime(source, (0, 0))), TEXT) == Raised("PermissionError")
    assert box.call(doing(lambda: os.setxattr(source, "user.x", b"1")), TEXT) == Raised(
        "PermissionError"
    )
    after = source.stat()
    assert (after.st_mode, after.st_mtime) == (before.st_mode, before.st_mtime)


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
        "limits": {
            "cpu_seconds": 1,
            "memory_bytes": 128 * MIB,
            "reply_bytes": 64 * MIB,
            "wall_seconds": 2,
        },
    }


def test_a_host_without_the_controls_cannot_build_a_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lacking() -> confine.Host:
        raise confine.ConfineError("platform", "the sandbox needs Linux, not plan9")

    monkeypatch.setattr(confine, "host", lacking)
    with pytest.raises(SandboxError, match="plan9"):
        Subprocess()


def test_the_landlock_floor_names_what_each_abi_cannot_guarantee() -> None:
    assert landlock_guarantees_lost(0) == (
        "create, remove, rename or hard-link a file or directory",
        "truncate a file, the source included",
    )
    # ABI 1 already blocks creation, removal, rename and relink; only truncation (ABI 3) is lost,
    # so ABI 1 and ABI 2 lose the same thing and share a degraded lineage.
    assert landlock_guarantees_lost(1) == ("truncate a file, the source included",)
    assert landlock_guarantees_lost(2) == landlock_guarantees_lost(1)
    assert landlock_guarantees_lost(REQUIRED_LANDLOCK_ABI) == ()
    assert landlock_guarantees_lost(8) == ()  # the floor met: nothing lost


@pytest.mark.skipif(
    confine.landlock_abi() >= REQUIRED_LANDLOCK_ABI, reason="this host needs no forced floor"
)
def test_a_real_sub_floor_host_would_fail_closed() -> None:
    # Where the real kernel is below the floor, the default sandbox refuses to build at all.
    with pytest.raises(SandboxError, match="allow_degraded_sandbox"):
        Subprocess()


def test_below_the_floor_the_sandbox_fails_closed_unless_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = confine.host()
    monkeypatch.setattr(confine, "host", lambda: confine.Host(real.arch, 0))
    with pytest.raises(SandboxError, match="Landlock ABI 0 is below"):
        Subprocess()  # fail closed: the source is not safe here
    degraded = Subprocess(allow_degraded=True)  # by explicit choice, recording what is lost
    assert degraded.lost_guarantees() == landlock_guarantees_lost(0)
    described = degraded.describe()
    assert described["landlock"] == 0 and described["degraded"] == list(degraded.lost_guarantees())
    assert isinstance(degraded.call(lambda: "fine", TEXT), Returned)  # it still runs


def test_allow_degraded_must_be_a_bool() -> None:
    with pytest.raises(TypeError, match="allow_degraded"):
        Subprocess(allow_degraded="yes")  # type: ignore[arg-type]


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
