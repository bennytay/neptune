"""``python -m docsite [OUT]``: build the site into ``OUT`` (default ``build/docs``).

See ``docsite.build``.
"""

import sys
from pathlib import Path

from docsite.build import build

ROOT = Path(__file__).resolve().parents[1]

out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "docs"
problems = build(ROOT, out.resolve())
for problem in problems:
    sys.stderr.write(f"docs: {problem}\n")
if not problems:
    sys.stdout.write(f"docs: built {out / 'html' / 'index.html'}\n")
raise SystemExit(1 if problems else 0)
