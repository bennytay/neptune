"""The ``neptune`` command line (ADR 0043): a thin layer over the SDK (``neptune.sdk``).

``neptune ingest <path>`` ingests a folder or one file into a package; ``--help`` lists
every option and the exit codes (``neptune.cli.exit_codes``).
"""

from neptune.cli.main import main, run

__all__ = ["main", "run"]
