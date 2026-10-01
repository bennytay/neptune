"""Source enumeration and probing: the ``Source`` interface, safe walking, scanning, readers,
sniffing, bounded container inspection and the probe engine. Grouping comes later (MVL-13).

- ``source``: ``Source`` and ``LocalSource``, the walk and its symlink / special-file policy.
- ``scan``: one deterministic pass: walk, digest, record revisions, reconcile absences.
- ``reader``: ``SourceReader`` implementations that hand one source's bytes to an adapter.
- ``sniff``: signatures and text classification; observations about bytes, never claims.
- ``containers``: what a zip, tar, gzip, bzip2 or xz holds, within ``ProbePolicy``.
- ``probe``: ``ProbeEngine``: every adapter's probe, the selection rule, containers, findings.
"""
