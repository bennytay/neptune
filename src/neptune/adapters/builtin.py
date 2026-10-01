"""The adapters Neptune ships. Adding a format is a new subpackage plus one line here.

Nothing in the runtime, the store or the model names a format: they see adapters only through the
registry and the contract. A job may also build its own ``AdapterRegistry`` from any adapters.
"""

from neptune.adapters.config import ConfigAdapter
from neptune.adapters.contract import Adapter
from neptune.adapters.registry import AdapterRegistry
from neptune.adapters.text import TextAdapter


def builtin_adapters() -> tuple[Adapter, ...]:
    """A fresh instance of every shipped adapter, in id order."""
    return (ConfigAdapter(), TextAdapter())


def default_registry() -> AdapterRegistry:
    """A registry of the shipped adapters."""
    return AdapterRegistry(builtin_adapters())
