"""The confinement: the seccomp program, run by a BPF interpreter for both architectures, the
syscall tables checked against the kernel's headers, and the controls a confined call reports."""

import errno
import json
import os
import re
import resource
import struct
from pathlib import Path
from typing import Final

import pytest

from neptune.runtime import confine
from neptune.runtime.confine import ARCHES, Arch, seccomp_program
from neptune.runtime.sandbox import Codec, Limits, Returned, Subprocess

ALLOW: Final = 0x7FFF0000
KILL: Final = 0x80000000
EPERM: Final = 0x00050000 | errno.EPERM
ENOSYS: Final = 0x00050000 | errno.ENOSYS
PID: Final = 4242
CLONE_THREAD: Final = 0x00010000
ARG1_LOW: Final = 24  # seccomp_data args[1], low word


def run(arch: Arch, nr: int, *args: int, audit: int | None = None) -> int:
    """What the filter returns for syscall ``nr`` with ``args``: a classic BPF interpreter for
    the instructions the filter uses."""
    data = struct.pack(
        "<iIQ6Q",
        nr,
        arch.audit if audit is None else audit,
        0,
        *(list(args) + [0] * (6 - len(args))),
    )
    program = seccomp_program(arch, PID)
    pc, acc = 0, 0
    for _ in range(len(program) + 1):
        code, jt, jf, k = program[pc]
        if code == 0x20:
            acc = struct.unpack_from("<I", data, k)[0]
            pc += 1
        elif code == 0x15:
            pc += 1 + (jt if acc == k else jf)
        elif code == 0x35:
            pc += 1 + (jt if acc >= k else jf)
        elif code == 0x45:
            pc += 1 + (jt if acc & k else jf)
        elif code == 0x06:
            return k
        else:
            raise AssertionError(f"unexpected instruction {code:#x}")
    raise AssertionError("the program did not return")


ARCH_LIST: Final = sorted(ARCHES.values(), key=lambda arch: arch.name)


@pytest.mark.parametrize("arch", ARCH_LIST, ids=lambda arch: arch.name)
def test_the_program_is_well_formed(arch: Arch) -> None:
    program = seccomp_program(arch, PID)
    assert len(program) < 4096  # BPF_MAXINSNS
    assert program[-1] == (0x06, 0, 0, ALLOW)
    for index, (code, jt, jf, _) in enumerate(program):
        if code in (0x15, 0x35, 0x45):
            assert index + 1 + max(jt, jf) < len(program)
    assert seccomp_program(arch, PID) == program  # deterministic


@pytest.mark.parametrize("arch", ARCH_LIST, ids=lambda arch: arch.name)
def test_denied_syscalls_are_refused_and_others_allowed(arch: Arch) -> None:
    denied = dict(arch.denied)
    assert len(set(denied.values())) == len(denied)  # one name per number
    for name, nr in arch.denied:
        absent = name in {"clone3", "io_uring_enter", "io_uring_register", "io_uring_setup"}
        assert run(arch, nr) == (ENOSYS if absent else EPERM), name
    allowed = set(range(0, 460)) - set(denied.values()) - {arch.clone}
    allowed -= {nr for _, nr in arch.self_only}
    for nr in sorted(allowed):
        assert run(arch, nr) == ALLOW, nr


def test_both_architectures_deny_the_same_syscalls() -> None:
    x86, arm = (set(dict(ARCHES[name].denied)) for name in ("x86_64", "aarch64"))
    # aarch64 has only clone, and only the modern *at/utimensat metadata forms; x86_64 also
    # carries the legacy chmod/chown/lchown/utime/utimes/futimesat numbers.
    assert x86 - arm == {
        "fork",
        "vfork",
        "chmod",
        "chown",
        "lchown",
        "utime",
        "utimes",
        "futimesat",
    }
    assert arm <= x86
    assert {"socket", "execve", "ptrace", "unshare", "bpf", "io_uring_setup"} <= arm
    # The metadata floor both share: no mode, owner, time or xattr change, and no fallocate.
    assert {"fchmod", "fchownat", "utimensat", "setxattr", "fremovexattr", "fallocate"} <= arm


@pytest.mark.parametrize("arch", ARCH_LIST, ids=lambda arch: arch.name)
def test_clone_makes_threads_never_processes(arch: Arch) -> None:
    assert run(arch, arch.clone, CLONE_THREAD | 0x3D0F00) == ALLOW  # pthread_create's flags
    assert run(arch, arch.clone, 0x11) == EPERM  # fork(): SIGCHLD only
    assert run(arch, arch.clone, 0x01200011) == EPERM  # glibc fork's flags


@pytest.mark.parametrize("arch", ARCH_LIST, ids=lambda arch: arch.name)
def test_signals_reach_only_this_process(arch: Arch) -> None:
    for _, nr in arch.self_only:
        assert run(arch, nr, PID, 9) == ALLOW
        assert run(arch, nr, PID + 1, 9) == EPERM
        assert run(arch, nr, 0, 9) == EPERM  # its process group
        assert run(arch, nr, 2**64 - 1, 9) == EPERM  # -1: every process
        assert run(arch, nr, PID | 1 << 32, 9) == EPERM  # the high word matters


@pytest.mark.parametrize("arch", ARCH_LIST, ids=lambda arch: arch.name)
def test_another_abi_is_killed(arch: Arch) -> None:
    assert run(arch, 0, audit=0x40000003) == KILL  # i386
    assert run(arch, 0, audit=0xC00000B7 if arch.name == "x86_64" else 0xC000003E) == KILL


def test_x32_syscalls_are_refused_on_x86_64() -> None:
    x86 = ARCHES["x86_64"]
    assert run(x86, 0x40000000 | 41) == ENOSYS
    assert run(x86, 0x40000000) == ENOSYS


HEADERS: Final = {
    "x86_64": Path("/usr/include/x86_64-linux-gnu/asm/unistd_64.h"),
    "aarch64": Path("/usr/include/asm-generic/unistd.h"),
}


@pytest.mark.parametrize("name", sorted(HEADERS))
def test_the_numbers_are_the_kernels(name: str) -> None:
    header = HEADERS[name]
    if not header.exists():
        pytest.skip(f"no kernel header at {header}")
    table = {
        match[1]: int(match[2])
        for match in re.finditer(r"#define __NR(?:3264)?_(\w+)\s+(\d+)", header.read_text())
    }
    arch = ARCHES[name]
    arg_denied = tuple((rule.name, rule.nr) for rule in arch.arg_denied)
    for syscall, nr in (*arch.denied, *arch.self_only, *arg_denied, ("clone", arch.clone)):
        assert table[syscall] == nr, syscall


@pytest.mark.parametrize("arch", ARCH_LIST, ids=lambda arch: arch.name)
def test_async_io_signal_ownership_and_dangerous_prctl_are_denied(arch: Arch) -> None:
    """The signal path the kernel delivers through descriptor ownership, which only Landlock ABI
    6 scopes, is refused on every ABI; a benign use of the same syscall is allowed."""
    denied = {rule.name: rule for rule in arch.arg_denied}
    fcntl, ioctl, prctl = denied["fcntl"], denied["ioctl"], denied["prctl"]
    assert fcntl.offset == ARG1_LOW
    for cmd in fcntl.values:  # F_SETOWN, F_SETSIG, F_SETOWN_EX
        assert run(arch, fcntl.nr, 5, cmd) == EPERM, cmd
    assert run(arch, fcntl.nr, 5, fcntl.values[0] | (1 << 32)) == EPERM  # the kernel truncates
    for allowed in (3, 4):  # F_GETFL, F_SETFL (os.set_blocking): untouched
        assert run(arch, fcntl.nr, 5, allowed) == ALLOW
    for request in ioctl.values:  # FIOSETOWN, SIOCSPGRP, FIOASYNC
        assert run(arch, ioctl.nr, 5, request) == EPERM, request
    assert run(arch, ioctl.nr, 5, 0x5401) == ALLOW  # TCGETS, a harmless ioctl
    for option in prctl.values:  # PR_SET_PDEATHSIG, PR_SET_DUMPABLE
        assert run(arch, prctl.nr, option) == EPERM, option
    assert run(arch, prctl.nr, 15) == ALLOW  # PR_SET_NAME: a thread may still name itself


@pytest.mark.parametrize("arch", ARCH_LIST, ids=lambda arch: arch.name)
def test_o_async_through_f_setfl_is_denied(arch: Arch) -> None:
    """``F_SETFL`` with ``O_ASYNC`` on a terminal aims SIGIO at the terminal's foreground group,
    the job's, with no owner set by the caller: refused on every ABI. Any other ``F_SETFL``, and
    ``O_ASYNC`` with any other command, is allowed."""
    fcntl_nr = next(rule.nr for rule in arch.arg_denied if rule.name == "fcntl")
    f_getfl, f_setfl, o_async = 3, 4, 0x2000
    assert o_async == os.O_ASYNC  # FASYNC, the same on x86_64 and aarch64 (asm-generic)
    for flags in (o_async, o_async | os.O_NONBLOCK | os.O_APPEND, o_async | 1 << 32):
        assert run(arch, fcntl_nr, 5, f_setfl, flags) == EPERM, hex(flags)
    assert run(arch, fcntl_nr, 5, f_setfl | 1 << 32, o_async) == EPERM  # the kernel truncates
    for flags in (0, os.O_NONBLOCK, os.O_NONBLOCK | os.O_APPEND, ~o_async & 0xFFFFFFFF, 1 << 45):
        assert run(arch, fcntl_nr, 5, f_setfl, flags) == ALLOW, hex(flags)  # os.set_blocking
    assert run(arch, fcntl_nr, 5, f_getfl, o_async) == ALLOW  # only F_SETFL sets the flag
    assert run(arch, fcntl_nr, 5, 1030, o_async) == ALLOW  # F_DUPFD_CLOEXEC, say: untouched


# --- A confined process, from the inside --------------------------------------------------------


def test_a_confined_call_runs_under_every_control() -> None:
    box = Subprocess(Limits(cpu_seconds=3, wall_seconds=10, memory_bytes=128 * 1024 * 1024))

    def controls() -> str:
        status = dict(
            line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
        )
        limits = {
            name: resource.getrlimit(getattr(resource, f"RLIMIT_{name}"))
            for name in ("AS", "CORE", "CPU", "FSIZE")
        }
        fds = sorted(int(p.name) for p in Path("/proc/self/fd").iterdir())
        return json.dumps(
            [
                status["Seccomp"].strip(),
                status["NoNewPrivs"].strip(),
                limits["CORE"],
                limits["CPU"],
                limits["FSIZE"],
                limits["AS"][0] == limits["AS"][1],
                fds,
            ]
        )

    outcome = box.call(controls, Codec(str, str.encode, bytes.decode))
    assert isinstance(outcome, Returned)
    seccomp, no_new_privs, core, cpu, fsize, as_fixed, fds = json.loads(outcome.value)
    assert (seccomp, no_new_privs) == ("2", "1")  # filter mode; no privilege can be gained
    assert core == [0, 0] and fsize == [0, 0] and cpu == [3, 4] and as_fixed
    assert fds[:3] == [0, 1, 2] and len(fds) <= 5  # stdio, the reply, the listing's own


def test_the_host_is_this_machine() -> None:
    host = confine.host()
    assert host.arch in ARCHES.values()
    assert host.landlock == confine.landlock_abi() >= 0
