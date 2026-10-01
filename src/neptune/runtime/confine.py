"""Kernel controls that confine one sandboxed adapter call on Linux (ADR 0030).

``confine`` runs in the forked child before any adapter code, and applies, in order:

1. ``PR_SET_PDEATHSIG``: the child is killed if the job's process dies first; then ``setsid`` so
   it has no controlling terminal: ``/dev/tty`` does not open, and ``TIOCSTI`` and
   ``TIOCSPGRP``, which need the caller's own controlling terminal, fail on any terminal. A
   terminal it can still open read-only (the job's ``/dev/pts/N``) is kept from raising SIGIO
   by the seccomp filter's ``O_ASYNC`` rules (5), not by ``setsid``.
2. Descriptors: standard input and output go to ``/dev/null`` and every other descriptor is
   closed except the ones the call keeps (the reply pipe, the source).
3. Resource limits: ``RLIMIT_CPU`` (SIGXCPU at the limit, SIGKILL a second later),
   ``RLIMIT_AS`` (the address space at fork plus the memory budget), ``RLIMIT_CORE`` 0 and not
   dumpable (a crash leaves no core dump holding source data), ``RLIMIT_FSIZE`` 0 (no byte is
   written to any file).
4. ``no_new_privs``, then Landlock where the kernel offers it (5.13+): no file or directory is
   created, written, truncated, renamed or removed; from ABI 4 no TCP bind or connect; from ABI
   6 no signal to a process outside the sandbox and no abstract Unix socket outside it.
5. A seccomp filter: no socket, no new process or program (``fork``, ``vfork``, ``clone``
   without ``CLONE_THREAD``, ``clone3``, ``execve``, ``execveat``), no signal to another
   process, no ``ptrace`` or cross-process memory or descriptor access, no namespaces, no
   ``bpf``, no ``io_uring``. No file-metadata change either (``chmod``, ``chown``, ``utimensat``
   and the ``*xattr`` family) and no ``fallocate``: Landlock covers none of those, so without
   the filter a parser could make the source unreadable, world-write another file, or defeat
   change detection even above the Landlock floor. Threads stay allowed. The filter also denies
   the async-I/O path to a signal the kernel delivers through descriptor ownership, which only
   Landlock ABI 6 scopes: ``fcntl`` ``F_SETOWN``/``F_SETOWN_EX``/``F_SETSIG``, ``fcntl``
   ``F_SETFL`` with ``O_ASYNC`` (on a terminal, which stays readable, the kernel itself makes
   the terminal's foreground process group the owner: the job's, in a shell), and ``ioctl``
   ``FIOSETOWN``/``SIOCSPGRP``/``FIOASYNC`` (EPERM), and ``prctl`` ``PR_SET_PDEATHSIG`` and
   ``PR_SET_DUMPABLE`` so a hostile child can neither outlive a killed job nor re-enable a
   core dump after confinement. These argument filters hold on every Landlock ABI.

Reading files stays allowed, so lazy imports, codecs and shared libraries keep working; ADR 0030
records why and what that leaves open. The syscall numbers below are the kernel's ``unistd``
tables for the two architectures the filter supports.
"""

import contextlib
import ctypes
import errno
import os
import platform
import resource
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NamedTuple

# --- prctl -------------------------------------------------------------------------------------

_PR_SET_PDEATHSIG: Final = 1
_PR_SET_DUMPABLE: Final = 4
_PR_SET_SECCOMP: Final = 22
_PR_SET_NO_NEW_PRIVS: Final = 38
_SECCOMP_MODE_FILTER: Final = 2

# --- classic BPF, as seccomp runs it ------------------------------------------------------------

_LD_W_ABS: Final = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ_K: Final = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_JGE_K: Final = 0x35  # BPF_JMP | BPF_JGE | BPF_K
_JSET_K: Final = 0x45  # BPF_JMP | BPF_JSET | BPF_K
_RET_K: Final = 0x06  # BPF_RET | BPF_K

_RET_KILL_PROCESS: Final = 0x80000000
_RET_ERRNO: Final = 0x00050000
_RET_ALLOW: Final = 0x7FFF0000

# struct seccomp_data { int nr; __u32 arch; __u64 instruction_pointer; __u64 args[6]; }
_NR: Final = 0
_ARCH: Final = 4
_ARG0_LOW: Final = 16  # little-endian: the low word first
_ARG0_HIGH: Final = 20
_ARG1_LOW: Final = 24  # args[1], its low word
_ARG2_LOW: Final = 32  # args[2], its low word

_CLONE_THREAD: Final = 0x00010000
_X32_SYSCALL_BIT: Final = 0x40000000

# Commands and requests that aim a signal through a descriptor's owner, the kernel's async-I/O
# path (``fcntl`` cmd, ``ioctl`` request) the ``kill``/``tkill`` family does not cover and only
# Landlock ABI 6 scopes. The kernel reads each as a 32-bit int, so matching the low word is
# enough (and necessary: a high-bit-padded value truncates to the same command).
_F_SETOWN: Final = 8
_F_SETSIG: Final = 10
_F_SETOWN_EX: Final = 15
_FIOSETOWN: Final = 0x8901
_SIOCSPGRP: Final = 0x8902
_FIOASYNC: Final = 0x5452
# ``F_SETFL`` with ``O_ASYNC`` (``FASYNC``, 0o20000 in asm-generic: the same on both architectures)
# reaches the same path with no owner set by the caller: on a terminal the kernel makes the
# terminal's foreground process group the owner, which is the job's when it runs in a shell. The
# flag argument is an int too, so its low word holds the bit. Every other ``F_SETFL`` stays
# allowed (``os.set_blocking``).
_F_SETFL: Final = 4
_O_ASYNC: Final = 0x2000
# ``prctl`` options a confined child must never reach after setup: clearing its parent-death
# signal (so it could outlive a killed job) or making itself dumpable again (a core dump holding
# source data). Every other option — a thread naming itself, say — stays allowed.
_PR_SET_PDEATHSIG_OPT: Final = 1
_PR_SET_DUMPABLE_OPT: Final = 4

# Answered ENOSYS, as if the kernel lacked them: libc then makes threads with ``clone``, and a
# library that would use io_uring falls back to plain reads. Everything else refused is EPERM.
_ABSENT: Final = frozenset({"clone3", "io_uring_enter", "io_uring_register", "io_uring_setup"})

# Numbers shared by every architecture (added to the kernel after the tables were unified).
_UNIFIED: Final = {
    "clone3": 435,
    "io_uring_enter": 426,
    "io_uring_register": 427,
    "io_uring_setup": 425,
    "pidfd_getfd": 438,
    "pidfd_send_signal": 424,
}

# A file's metadata is not covered by any Landlock ABI, and ``fallocate`` punches or zeroes bytes
# without growing a file, so ``RLIMIT_FSIZE`` 0 does not stop it. An adapter only reads its source,
# so these are refused outright: without them "the source is immutable" would be false even on a
# host above the Landlock floor (a parser could ``chmod`` the source unreadable, or world-write a
# user's ``.bashrc``, or ``utimensat`` it to defeat change detection). aarch64 has only the modern
# ``*at`` and ``utimensat`` forms; x86_64 also carries the legacy numbers.
_METADATA_X86_64: Final = {
    "chmod": 90,
    "chown": 92,
    "fallocate": 285,
    "fchmod": 91,
    "fchmodat": 268,
    "fchown": 93,
    "fchownat": 260,
    "fremovexattr": 199,
    "fsetxattr": 190,
    "futimesat": 261,
    "lchown": 94,
    "lremovexattr": 198,
    "lsetxattr": 189,
    "removexattr": 197,
    "setxattr": 188,
    "utime": 132,
    "utimensat": 280,
    "utimes": 235,
}
_METADATA_AARCH64: Final = {
    "fallocate": 47,
    "fchmod": 52,
    "fchmodat": 53,
    "fchown": 55,
    "fchownat": 54,
    "fremovexattr": 16,
    "fsetxattr": 7,
    "lremovexattr": 15,
    "lsetxattr": 6,
    "removexattr": 14,
    "setxattr": 5,
    "utimensat": 88,
}


class ArgRule(NamedTuple):
    """A syscall that is allowed but answers EPERM for some argument values.

    EPERM when the argument whose low word sits at ``offset`` is one of ``values``, or, given a
    ``flag`` ``(value, flag_offset, bits)``, when it is ``value`` and the argument whose low word
    sits at ``flag_offset`` has any of ``bits`` set.
    """

    name: str
    nr: int
    offset: int
    values: tuple[int, ...]
    flag: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class Arch:
    """One architecture's audit tag and syscall numbers (``asm/unistd_64.h``, ``unistd.h``)."""

    name: str
    audit: int
    x32: bool  # syscalls with the x32 bit share x86_64's audit tag and must be refused
    denied: tuple[tuple[str, int], ...]  # refused outright, by name and number
    clone: int  # allowed only with CLONE_THREAD: a thread, never a process
    self_only: tuple[tuple[str, int], ...]  # kill and tgkill: only towards this process
    arg_denied: tuple[ArgRule, ...]  # allowed, but EPERM for a few argument values


ARCHES: Final = {
    "x86_64": Arch(
        name="x86_64",
        audit=0xC000003E,
        x32=True,
        denied=tuple(
            sorted(
                (
                    _UNIFIED
                    | _METADATA_X86_64
                    | {
                        "bpf": 321,
                        "execve": 59,
                        "execveat": 322,
                        "fork": 57,
                        "process_vm_readv": 310,
                        "process_vm_writev": 311,
                        "ptrace": 101,
                        "rt_sigqueueinfo": 129,
                        "rt_tgsigqueueinfo": 297,
                        "setns": 308,
                        "socket": 41,
                        "tkill": 200,
                        "unshare": 272,
                        "vfork": 58,
                    }
                ).items()
            )
        ),
        clone=56,
        self_only=(("kill", 62), ("tgkill", 234)),
        arg_denied=(
            ArgRule(
                "fcntl",
                72,
                _ARG1_LOW,
                (_F_SETOWN, _F_SETSIG, _F_SETOWN_EX),
                (_F_SETFL, _ARG2_LOW, _O_ASYNC),
            ),
            ArgRule("ioctl", 16, _ARG1_LOW, (_FIOSETOWN, _SIOCSPGRP, _FIOASYNC)),
            ArgRule("prctl", 157, _ARG0_LOW, (_PR_SET_PDEATHSIG_OPT, _PR_SET_DUMPABLE_OPT)),
        ),
    ),
    "aarch64": Arch(
        name="aarch64",
        audit=0xC00000B7,
        x32=False,
        denied=tuple(
            sorted(
                (
                    _UNIFIED
                    | _METADATA_AARCH64
                    | {
                        "bpf": 280,
                        "execve": 221,
                        "execveat": 281,
                        "process_vm_readv": 270,
                        "process_vm_writev": 271,
                        "ptrace": 117,
                        "rt_sigqueueinfo": 138,
                        "rt_tgsigqueueinfo": 240,
                        "setns": 268,
                        "socket": 198,
                        "tkill": 130,
                        "unshare": 97,
                    }
                ).items()
            )
        ),
        clone=220,
        self_only=(("kill", 129), ("tgkill", 131)),
        arg_denied=(
            ArgRule(
                "fcntl",
                25,
                _ARG1_LOW,
                (_F_SETOWN, _F_SETSIG, _F_SETOWN_EX),
                (_F_SETFL, _ARG2_LOW, _O_ASYNC),
            ),
            ArgRule("ioctl", 29, _ARG1_LOW, (_FIOSETOWN, _SIOCSPGRP, _FIOASYNC)),
            ArgRule("prctl", 167, _ARG0_LOW, (_PR_SET_PDEATHSIG_OPT, _PR_SET_DUMPABLE_OPT)),
        ),
    ),
}

# --- Landlock (linux/landlock.h) ---------------------------------------------------------------

_LANDLOCK_CREATE_RULESET: Final = 444
_LANDLOCK_RESTRICT_SELF: Final = 446
_LANDLOCK_CREATE_RULESET_VERSION: Final = 1

# Every filesystem right that changes something, by the ABI that introduced it. Execute and the
# two read rights are left unhandled: reads stay open, and seccomp refuses execve.
_FS_WRITE_V1: Final = sum(
    1 << bit
    for bit in (
        1,  # WRITE_FILE
        4,  # REMOVE_DIR
        5,  # REMOVE_FILE
        6,  # MAKE_CHAR
        7,  # MAKE_DIR
        8,  # MAKE_REG
        9,  # MAKE_SOCK
        10,  # MAKE_FIFO
        11,  # MAKE_BLOCK
        12,  # MAKE_SYM
    )
)
_FS_REFER: Final = 1 << 13  # ABI 2
_FS_TRUNCATE: Final = 1 << 14  # ABI 3
_FS_IOCTL_DEV: Final = 1 << 15  # ABI 5
_NET_TCP: Final = (1 << 0) | (1 << 1)  # ABI 4: BIND_TCP, CONNECT_TCP
_SCOPE_ALL: Final = (1 << 0) | (1 << 1)  # ABI 6: ABSTRACT_UNIX_SOCKET, SIGNAL


class _RulesetAttr(ctypes.Structure):
    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    )


class _SockFilter(ctypes.Structure):
    _fields_ = (
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    )


class _SockFprog(ctypes.Structure):
    _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter)))


class ConfineError(Exception):
    """A control could not be applied; ``control`` names it. No adapter code has run."""

    def __init__(self, control: str, detail: str) -> None:
        super().__init__(f"{control}: {detail}")
        self.control = control
        self.detail = detail


@dataclass(frozen=True)
class Host:
    """What this host enforces: its architecture's filter and its Landlock ABI (0: none)."""

    arch: Arch
    landlock: int


_LIBC: list[ctypes.CDLL] = []


def _c() -> ctypes.CDLL:
    """libc, loaded once (in the parent, by ``host``) so the child only calls into it."""
    if not _LIBC:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = (
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        )
        libc.prctl.restype = ctypes.c_int
        libc.syscall.restype = ctypes.c_long
        _LIBC.append(libc)
    return _LIBC[0]


def _fail(control: str) -> ConfineError:
    code = ctypes.get_errno()
    return ConfineError(control, errno.errorcode.get(code, str(code)))


def _prctl(control: str, option: int, arg: int = 0) -> None:
    if _c().prctl(option, arg, 0, 0, 0) != 0:
        raise _fail(control)


def landlock_abi() -> int:
    """The kernel's Landlock ABI version, or 0 where Landlock is not built in or not enabled."""
    abi = _c().syscall(
        ctypes.c_long(_LANDLOCK_CREATE_RULESET),
        ctypes.c_void_p(None),
        ctypes.c_ulong(0),
        ctypes.c_ulong(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    return max(int(abi), 0)


def host() -> Host:
    """This host's controls, or ``ConfineError`` naming what it lacks. Call in the parent."""
    if sys.platform != "linux":
        raise ConfineError("platform", f"the sandbox needs Linux, not {sys.platform}")
    machine = platform.machine()
    arch = ARCHES.get(machine)
    if arch is None or ctypes.sizeof(ctypes.c_void_p) != 8 or sys.byteorder != "little":
        raise ConfineError("seccomp", f"no syscall filter for {machine} in a 64-bit process")
    if not hasattr(os, "pidfd_open"):
        raise ConfineError("pidfd", "os.pidfd_open is missing")
    return Host(arch, landlock_abi())


def seccomp_program(arch: Arch, pid: int) -> list[tuple[int, int, int, int]]:
    """The seccomp filter for process ``pid``, as BPF ``(code, jt, jf, k)``.

    Each block tests the syscall number held in the accumulator and ends in returns, so a block
    is reached only when every block before it did not match.
    """
    program = [
        (_LD_W_ABS, 0, 0, _ARCH),
        (_JEQ_K, 1, 0, arch.audit),
        (_RET_K, 0, 0, _RET_KILL_PROCESS),  # another ABI's numbers would mean other syscalls
        (_LD_W_ABS, 0, 0, _NR),
    ]
    if arch.x32:
        program += [(_JGE_K, 0, 1, _X32_SYSCALL_BIT), (_RET_K, 0, 0, _RET_ERRNO | errno.ENOSYS)]
    for name, nr in arch.denied:
        code = errno.ENOSYS if name in _ABSENT else errno.EPERM
        program += [(_JEQ_K, 0, 1, nr), (_RET_K, 0, 0, _RET_ERRNO | code)]
    program += [
        (_JEQ_K, 0, 4, arch.clone),
        (_LD_W_ABS, 0, 0, _ARG0_LOW),
        (_JSET_K, 1, 0, _CLONE_THREAD),
        (_RET_K, 0, 0, _RET_ERRNO | errno.EPERM),
        (_RET_K, 0, 0, _RET_ALLOW),
    ]
    for _, nr in arch.self_only:  # kill(pid, ...) and tgkill(tgid, ...): arg 0 is this process
        program += [
            (_JEQ_K, 0, 6, nr),
            (_LD_W_ABS, 0, 0, _ARG0_LOW),
            (_JEQ_K, 0, 3, pid),
            (_LD_W_ABS, 0, 0, _ARG0_HIGH),
            (_JEQ_K, 0, 1, 0),
            (_RET_K, 0, 0, _RET_ALLOW),
            (_RET_K, 0, 0, _RET_ERRNO | errno.EPERM),
        ]
    for rule in arch.arg_denied:  # EPERM only for the named argument values
        flagged: list[tuple[int, int, int, int]] = []
        if rule.flag is not None:  # this value, with a refused bit in another argument: EPERM
            value, flag_offset, bits = rule.flag
            flagged = [
                (_JEQ_K, 0, 2, value),  # another value: allowed
                (_LD_W_ABS, 0, 0, flag_offset),  # the flag argument's low word
                (_JSET_K, 1, 0, bits),  # a refused bit set jumps to the EPERM return
            ]
        count = len(rule.values)
        program.append((_JEQ_K, 0, count + len(flagged) + 3, rule.nr))  # another syscall: skip
        program.append((_LD_W_ABS, 0, 0, rule.offset))  # the argument's low word
        for index, value in enumerate(rule.values):  # a match jumps to the EPERM return
            program.append((_JEQ_K, count - index + len(flagged), 0, value))
        program += flagged
        program.append((_RET_K, 0, 0, _RET_ALLOW))  # this syscall, an argument we allow
        program.append((_RET_K, 0, 0, _RET_ERRNO | errno.EPERM))
    program.append((_RET_K, 0, 0, _RET_ALLOW))
    return program


def _seccomp(arch: Arch) -> None:
    program = seccomp_program(arch, os.getpid())
    instructions = (_SockFilter * len(program))(*(_SockFilter(*step) for step in program))
    fprog = _SockFprog(len(program), instructions)
    if _c().prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.addressof(fprog), 0, 0) != 0:
        raise _fail("seccomp")


def _landlock(abi: int) -> None:
    if abi < 1:
        return
    fs = _FS_WRITE_V1
    if abi >= 2:
        fs |= _FS_REFER
    if abi >= 3:
        fs |= _FS_TRUNCATE
    if abi >= 5:
        fs |= _FS_IOCTL_DEV
    attr = _RulesetAttr(fs, _NET_TCP if abi >= 4 else 0, _SCOPE_ALL if abi >= 6 else 0)
    ruleset = _c().syscall(
        ctypes.c_long(_LANDLOCK_CREATE_RULESET),
        ctypes.byref(attr),
        ctypes.c_ulong(ctypes.sizeof(attr)),
        ctypes.c_ulong(0),
    )
    if ruleset < 0:
        raise _fail("landlock")
    try:
        restricted = _c().syscall(
            ctypes.c_long(_LANDLOCK_RESTRICT_SELF), ctypes.c_long(ruleset), ctypes.c_ulong(0)
        )
        if restricted != 0:
            raise _fail("landlock")
    finally:
        os.close(ruleset)


def _lower(kind: int, soft: int, hard: int) -> None:
    """Set a limit to ``soft``/``hard``, never above a hard limit the process already has."""
    _, current = resource.getrlimit(kind)
    if current != resource.RLIM_INFINITY:
        hard = min(hard, current)
    resource.setrlimit(kind, (min(soft, hard), hard))


def _address_space() -> int:
    pages = int(Path("/proc/self/statm").read_bytes().split()[0])
    return pages * os.sysconf("SC_PAGE_SIZE")


def _descriptors(keep: frozenset[int]) -> None:
    """Standard streams to ``/dev/null`` (unless kept); close every other descriptor not kept.

    The open descriptors are listed from ``/proc/self/fd``: closing every number up to
    ``RLIMIT_NOFILE`` one by one costs tens of milliseconds where that limit is large. Python's
    own ``sys`` streams are rebound to the standard descriptors, since the objects inherited
    may write to a descriptor now closed (a test runner's capture file, a log).
    """
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        if fd not in keep and fd != null:
            os.dup2(null, fd)
    still_open = keep | {0, 1, 2}
    listed = [int(entry.name) for entry in Path("/proc/self/fd").iterdir()]  # read whole first
    for fd in listed:
        if fd not in still_open:
            with contextlib.suppress(OSError):  # the listing's own descriptor, closed already
                os.close(fd)
    sys.stdin = os.fdopen(0, "r", closefd=False)
    sys.stdout = os.fdopen(1, "w", closefd=False)
    sys.stderr = os.fdopen(2, "w", closefd=False)


def confine(
    host: Host, keep: frozenset[int], parent: int, cpu_seconds: int, memory_bytes: int
) -> None:
    """Confine this forked process; ``ConfineError`` if any control cannot be applied."""
    try:
        _prctl("pdeathsig", _PR_SET_PDEATHSIG, signal.SIGKILL)
        if os.getppid() != parent:  # the job died before the line above took effect
            os._exit(1)
        # A new session, with no controlling terminal (a forked child inherits the job's): then
        # ``/dev/tty`` does not open, and ``TIOCSTI`` input injection and ``TIOCSPGRP`` fail on
        # any terminal, since both need the caller's own controlling one. It does not stop
        # SIGIO: reads stay open, so the job's ``/dev/pts/N`` still opens read-only, and
        # ``O_ASYNC`` on it would aim SIGIO at the terminal's foreground group, the job's. The
        # seccomp filter refuses ``O_ASYNC`` (``F_SETFL``, ``FIOASYNC``) on every Landlock ABI;
        # ABI 6 signal scoping blocks that delivery as well.
        os.setsid()
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGXCPU):
            signal.signal(signum, signal.SIG_DFL)
        signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # a write past RLIMIT_FSIZE raises
        _descriptors(keep)
        _lower(resource.RLIMIT_CORE, 0, 0)
        _lower(resource.RLIMIT_FSIZE, 0, 0)
        _lower(resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 1)
        space = _address_space() + memory_bytes
        _lower(resource.RLIMIT_AS, space, space)
    except (OSError, ValueError) as exc:
        raise ConfineError("process", type(exc).__name__) from exc
    # Not dumpable: a crash never reaches the core-dump path, where a piped core_pattern
    # (apport, systemd-coredump) would be handed the memory, and the source data, regardless
    # of RLIMIT_CORE, and would hold the dying process for a second or more.
    _prctl("dumpable", _PR_SET_DUMPABLE, 0)
    _prctl("no_new_privs", _PR_SET_NO_NEW_PRIVS, 1)
    _landlock(host.landlock)
    _seccomp(host.arch)
