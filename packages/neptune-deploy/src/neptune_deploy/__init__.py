"""Neptune Deploy: deployment lifecycle evidence, added to the compiler as a plugin.

Deploy adds adapters and read-only ``Source``s to the compiler through its entry points, and
nothing else; the lifecycle record kinds it emits are the compiler model's (ADR 0001). See this
package's AGENTS.md (rules) and ARCHITECTURE.md (diagram).
"""

from typing import Final

__version__ = "0.0.1"

# The package schema Deploy is built against: version 4 added the lifecycle kinds (root ADR 0051),
# version 5 the assertion kind (root ADR 0062), version 6 the civil time zone kind and lifecycle
# list states (root ADR 0061), version 7 the task kinds (root ADR 0063; Deploy ignores them),
# version 8 the robot-description kinds (root ADR 0039) and version 10 the status and
# safety-state kinds (root ADR 0071), which change none. contracts/lock.toml declares the same
# version as package-schema 10.0.0 (ADR 0001 §3).
PACKAGE_SCHEMA_VERSION: Final = 10
