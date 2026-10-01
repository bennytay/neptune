"""Format adapters: the contract, the registry, and one leaf subpackage per format (ADR 0008, 0024).

- ``contract``: what an adapter is and the plain data it exchanges with the runtime.
- ``registry``: the adapters a job may use, and the rule that picks one for a source.
- ``check``: the contract's laws as checks, run by ``harness`` and by every adapter's tests.
- ``harness``: one adapter over one source, in process: plan, ingest every chunk, check.
- ``builtin``: the adapters Neptune ships. ``text`` is the reference adapter to copy.

A format subpackage imports only ``neptune.model``, ``neptune.identity`` and
``neptune.adapters.contract``: never another adapter, the registry or the runtime.
"""
