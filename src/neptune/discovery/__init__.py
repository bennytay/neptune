"""Source enumeration and probing: the ``Source`` interface, safe walking, scanning, readers,
sniffing, bounded container inspection, the probe engine, and the observed layout grouping reads.

- ``source``: ``Source`` and ``LocalSource``, the walk and its symlink / special-file policy.
- ``scan``: one deterministic pass: walk, digest, record revisions, reconcile absences.
- ``reader``: ``SourceReader`` implementations that hand one source's bytes to an adapter.
- ``sniff``: signatures and text classification; observations about bytes, never claims.
- ``containers``: what a zip, tar, gzip, bzip2 or xz holds, within ``ProbePolicy``.
- ``probe``: ``ProbeEngine``: every adapter's probe, the selection rule, containers, findings.
- ``layout``: a scan's files and links and what their names state; observations only. The
  sessions they suggest are inferred in ``neptune.derived.grouping`` (ADR 0036).
"""
