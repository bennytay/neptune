"""The lakehouse: a view over packages' series Parquet, read where the packages lie (ADR 0013).

``store`` is the read-only ``ObjectStore`` a package's objects are reached through (a local
directory or an S3-compatible bucket). ``series`` maps registered streams to their series files
and the settings their manifests record. ``read`` scans those files in place with DuckDB or
DataFusion, pushing time windows down to the scan, and returns one Arrow table. No package byte
is copied.

``evidence`` resolves an evidence reference to the cited bytes, verified per chunk; ``decode``
extracts frames, regions, pages, rows and values from them; ``media`` stores those derivatives
in the tenant's Lance table and hydrates references lazily (ADR 0014).
"""
