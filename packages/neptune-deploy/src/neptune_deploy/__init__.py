"""Neptune Deploy: deployment lifecycle evidence, added to the compiler as a plugin.

Deploy adds adapters and read-only ``Source``s to the compiler through its entry points, and
nothing else; the lifecycle record kinds it emits are the compiler model's (ADR 0001). See this
package's AGENTS.md (rules) and ARCHITECTURE.md (diagram).
"""

from typing import Final

__version__ = "0.0.1"

# The package schema Deploy is built against: version 4 holds the lifecycle kinds (root ADR 0051).
# contracts/lock.toml declares the same version as package-schema 4.0.0 (ADR 0001 §3).
PACKAGE_SCHEMA_VERSION: Final = 5
