"""Installed plugin distributions for the entry-point loader's tests (ADR 0058).

``install(site, ...)`` writes what ``pip install`` leaves in a site directory: a
``<name>-<version>.dist-info`` with ``METADATA`` and ``entry_points.txt``, and the distribution's
modules beside it. ``importlib.metadata`` reads them exactly as it reads any installed package,
and ``import`` loads the modules once the site is on ``sys.path``.

The plugins are real adapters from ``tests/fixtures/adapters`` (``tally``, ``framelog``), copied
into a module of each distribution, and broken ones: a module that does not import, a factory that
raises, an object that is no adapter, an adapter under another entry-point name, another ABI.
"""

import re
from pathlib import Path
from typing import Final

ADAPTERS: Final = Path(__file__).parents[1] / "adapters"

TALLY: Final = (ADAPTERS / "tally_adapter.py").read_text(encoding="utf-8")
FRAMELOG: Final = (ADAPTERS / "framelog_adapter.py").read_text(encoding="utf-8")

# A module whose import fails, as a plugin with a missing dependency does.
BROKEN_IMPORT: Final = "import neptune_test_dependency_that_is_not_installed  # noqa: F401\n"

# A factory that raises when it is called, with a path in its message that must not leak.
RAISING_FACTORY: Final = """
def make():
    raise RuntimeError("cannot read /home/someone/.config/plugin.toml")
"""

NOT_AN_ADAPTER: Final = """
class NotAnAdapter:
    descriptor = None

answer = 42
"""

# The tally adapter at another ABI version: the registry would refuse it, so the loader does.
OTHER_ABI: Final = (
    TALLY.replace("abi=ABI_VERSION", "abi=ABI_VERSION + 1")
    .replace('id="tally"', 'id="tally_next"')
    .replace('"tally.bad_row"', '"tally_next.bad_row"')
)


def module_name(distribution: str) -> str:
    return re.sub(r"[-.]+", "_", distribution).lower()


def install(
    site: Path,
    distribution: str,
    version: str,
    *,
    adapters: dict[str, str] | None = None,
    sources: dict[str, str] | None = None,
    module: str | None = None,
) -> Path:
    """Install ``distribution`` at ``version`` into ``site``; its package holds ``module``.

    ``adapters`` and ``sources`` map entry-point names to values, ``module:attribute`` where
    ``module`` is relative to the distribution's own package (``""`` for the package itself).
    """
    package = module_name(distribution)
    info = site / f"{package}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n", encoding="utf-8"
    )
    lines: list[str] = []
    for group, points in (("neptune.adapters", adapters), ("neptune.sources", sources)):
        if points:
            lines.append(f"[{group}]")
            for name, value in points.items():
                where, _, attribute = value.partition(":")
                target = f"{package}.{where}" if where else package
                lines.append(f"{name} = {target}:{attribute}")
            lines.append("")
    (info / "entry_points.txt").write_text("\n".join(lines), encoding="utf-8")
    (site / package).mkdir()
    (site / package / "__init__.py").write_text(module or "", encoding="utf-8")
    return site / package
