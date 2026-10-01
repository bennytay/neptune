"""Ingest through the SDK and SIGKILL the process mid-job: the SDK resume test's victim.

Usage: ``python sdk_victim.py ROOT DESTINATION HOME PROGRESS COUNT``

Runs ``Neptune(HOME).ingest(ROOT, DESTINATION)`` with the shipped adapters and the default
sandbox. Every event is appended to ``PROGRESS`` as one canonical JSON line and flushed to disk
before the job goes on, so the parent can read what the job did before it died. Just after the
``COUNT``-th chunk this job commits (not one it reused), the process kills itself with SIGKILL:
no handler runs and nothing is cleaned up, the way a crashed machine or an OOM killer ends a job.
"""

import os
import signal
import sys
from pathlib import Path

from neptune.identity import canonical_json
from neptune.sdk import JobEvent, Neptune


def main(argv: list[str]) -> int:
    root, destination, home, progress = (Path(arg) for arg in argv[:4])
    count = int(argv[4])
    committed = 0
    with progress.open("ab") as log:

        def on_event(event: JobEvent) -> None:
            nonlocal committed
            log.write(canonical_json.dumps(event.to_json()) + b"\n")
            log.flush()
            os.fsync(log.fileno())
            if event.kind == "chunk_committed" and event.details["new"]:
                committed += 1
                if committed == count:
                    os.kill(os.getpid(), signal.SIGKILL)

        result = Neptune(home).ingest(root, destination, on_event=on_event)
    return 0 if result.committed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
