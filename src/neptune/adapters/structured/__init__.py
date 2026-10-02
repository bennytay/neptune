"""Readers for structured text (JSON, TOML, YAML) that more than one adapter shares (ADR 0055).

This is not an adapter. It holds what the ``config`` adapter (ADR 0037) first wrote and the
``calibration`` adapter (ADR 0055) also needs: decoding a file to text, telling the formats apart
by their bytes, reading each into documents of nodes with exact spans, and the limits and
findings that make hostile input cost findings rather than exceptions. Adapters never import each
other (ADR 0008 §4); both import this, which imports only the model, identity and the contract.

Nothing here knows a record kind or a finding code: ``load.problems`` returns names
(``too_large``, ``syntax_error``) the adapter prefixes with its own id.
"""
