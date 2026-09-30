# Provenance fixtures

- `arm_limits.csv`: a register a source might ship. `tests/integration/test_provenance_lineage.py`
  cites its cells by `RowCell` and `ByteRange` and resolves every citation back to the bytes.
