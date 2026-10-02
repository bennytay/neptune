"""Read-only connectors: the compiler's ``neptune.sources`` entry points (ADR 0001 §4).

A connector lists what an external system holds and hands its bytes to the compiler. It never
writes, acknowledges or transitions anything it reads, and it uses the network only after the
workspace's ``require_network`` allows it. ``object_store`` is the first (ADR 0006).
"""
