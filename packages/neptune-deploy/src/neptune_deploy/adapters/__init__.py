"""Deploy's format adapters, one leaf subpackage per lifecycle source format (ADR 0001).

Each follows the compiler's four-method ABI (root ADRs 0008, 0024) and runs in its sandbox: it
imports only ``neptune.model``, ``neptune.identity`` and ``neptune.adapters.contract``, reads only
the ``SourceReader`` it is given, and never touches the network or the filesystem. Each is
registered under the ``neptune.adapters`` entry point in ``pyproject.toml``.
"""
