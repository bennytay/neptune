-- 0004 record bodies and Unknown pointers (Ledger ADR 0008 §1, §2).
--
-- Applied after 0003 in the same tenant schema, with search_path set to that schema alone.
-- Columns are added to the partitioned parent, so every partition, present or generated later,
-- has them. Nothing here is generated: the hot-filter projections are migration 0005 onwards,
-- generated from the package schema by neptune_ledger.catalog.projection.

-- 0. Rows registered before this migration would get no body and no Unknown pointers: blanks
-- that read as "no Unknown field". record is append-only, so they cannot be filled in; such a
-- catalog is rebuilt from its packages and registration log instead (ADR 0002 §4, ADR 0008 §1).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM record) THEN
    RAISE EXCEPTION 'record rows registered before migration 0004 would lack bodies and Unknown'
      ' pointers; rebuild this catalog from its packages and registration log (ADR 0008)';
  END IF;
END
$$;

-- 1. The record's canonical JSON as jsonb, for projection only (ADR 0008 §1). The package line
-- stays authoritative and body_digest pins it; jsonb does not keep the canonical bytes. NULL
-- only when a string or key of the record holds U+0000, which jsonb cannot store: the body is
-- then read from the package, never guessed.
ALTER TABLE record ADD COLUMN body jsonb CHECK (body IS NULL OR jsonb_typeof(body) = 'object');

-- 2. JSON pointers, into the record, of every field whose state is Unknown (ADR 0008 §2), by the
-- same walk as ambiguous_pointers.
ALTER TABLE record ADD COLUMN unknown_pointers text[] NOT NULL DEFAULT '{}';
CREATE INDEX record_by_unknown ON record USING gin (unknown_pointers)
  WHERE unknown_pointers <> '{}';
