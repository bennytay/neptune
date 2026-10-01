"""The compose stack and whether it is reachable.

A stage declares ``needs_services``; it matters only when that stage runs for real. The harness
never starts Docker by itself: ``--compose`` does, and otherwise a real stage that needs the
services fails with the command to start them.
"""

from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

COMPOSE_FILE: Final = Path(__file__).resolve().parent / "compose.yaml"
START_COMMAND: Final = f"docker compose -f {COMPOSE_FILE.parent.name}/compose.yaml up -d --wait"
DEFAULTS: Final = {"HARNESS_POSTGRES": "127.0.0.1:55432", "HARNESS_MINIO": "127.0.0.1:59000"}


@dataclass(frozen=True)
class Endpoint:
    name: str
    host: str
    port: int

    def reachable(self, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return True
        except OSError:
            return False


def endpoints(environ: Mapping[str, str] | None = None) -> list[Endpoint]:
    """Postgres and MinIO, at the compose ports unless ``HARNESS_POSTGRES`` / ``HARNESS_MINIO``."""
    env = os.environ if environ is None else environ
    found: list[Endpoint] = []
    for variable, default in sorted(DEFAULTS.items()):
        host, _, port = env.get(variable, default).rpartition(":")
        found.append(Endpoint(variable.removeprefix("HARNESS_").lower(), host, int(port)))
    return found


def unreachable(environ: Mapping[str, str] | None = None) -> list[str]:
    """The names of the services that do not answer."""
    return [e.name for e in endpoints(environ) if not e.reachable()]


def compose(action: str) -> int:
    """``up`` (wait until healthy) or ``down`` (and remove volumes) for the harness stack."""
    args = {"up": ["up", "-d", "--build", "--wait"], "down": ["down", "--volumes"]}[action]
    command = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(command, check=False).returncode
