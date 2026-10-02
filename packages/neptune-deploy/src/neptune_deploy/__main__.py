"""``python -m neptune_deploy``: Deploy's command line (ADR 0002 §8)."""

import sys

from neptune_deploy.lifecycle.cli import main

if __name__ == "__main__":
    sys.exit(main())
