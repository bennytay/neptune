-- 0012 configuration snapshots open their anchored configuration thread (Ledger ADR 0017).
--
-- Applied in the same tenant schema, with search_path set to that schema alone. ADR 0017 adds
-- configuration_snapshot to ADR 0003 §2's configuration row: registration now writes a thread
-- and a subject thread_member row for each snapshot. The thread tables keep their shape; the
-- 'configuration' thread kind already exists (0007).

-- 0. A catalog that already holds configuration_snapshot records indexed them under the old
-- membership, with no thread rows. The thread tables are append-only and filled at registration
-- from the verified lines (ADR 0010 §1), so they cannot be filled in here: such a catalog is
-- rebuilt from its packages and registration log instead (ADR 0012), which writes the new rows.
-- A catalog holding none needs nothing: its index already equals what a rebuild would give.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM record WHERE kind = 'configuration_snapshot') THEN
    RAISE EXCEPTION 'configuration_snapshot records registered before migration 0012 are in no'
      ' thread; rebuild this catalog from its packages and registration log (ADR 0017)';
  END IF;
END
$$;
