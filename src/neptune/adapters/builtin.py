"""The adapters Neptune ships. Adding a format is a new subpackage plus one line here.

Nothing in the runtime, the store or the model names a format: they see adapters only through the
registry and the contract. A job may also build its own ``AdapterRegistry`` from any adapters.
"""

from neptune.adapters.config import ConfigAdapter
from neptune.adapters.contract import Adapter
from neptune.adapters.flightlog import FlightLogAdapter
from neptune.adapters.image import ImageAdapter
from neptune.adapters.markdown import MarkdownAdapter
from neptune.adapters.mcap import McapAdapter
from neptune.adapters.pdf import PdfAdapter
from neptune.adapters.registry import AdapterRegistry
from neptune.adapters.rosbag1 import Rosbag1Adapter
from neptune.adapters.rosbag2 import Rosbag2Adapter
from neptune.adapters.software import SoftwareAdapter
from neptune.adapters.tabular import TabularAdapter
from neptune.adapters.text import TextAdapter


def builtin_adapters() -> tuple[Adapter, ...]:
    """A fresh instance of every shipped adapter, in id order."""
    return (
        ConfigAdapter(),
        FlightLogAdapter(),
        ImageAdapter(),
        MarkdownAdapter(),
        McapAdapter(),
        PdfAdapter(),
        Rosbag1Adapter(),
        Rosbag2Adapter(),
        SoftwareAdapter(),
        TabularAdapter(),
        TextAdapter(),
    )


def default_registry() -> AdapterRegistry:
    """A registry of the shipped adapters."""
    return AdapterRegistry(builtin_adapters())
