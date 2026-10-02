-- 0006 the derived entity-thread index and ordered transform upstream (Ledger ADRs 0003, 0010).
--
-- Applied after 0005 in the same tenant schema, with search_path set to that schema alone.
-- Thread membership needs record bodies (Stream.run, a component's category, a software item's
-- commit, Ambiguous candidates), so registration computes it from the verified lines and writes
-- it here in the same transaction as the record rows (ADR 0010 §1). Every row is a function of
-- the package and the Ledger version; registration_key is the registering package's tx_seq, so a
-- replay of the registration log rebuilds these tables byte for byte (ADR 0003 §8).

-- 0. Packages registered before this migration have no thread rows and no upstream positions.
-- Every table here is append-only, so they cannot be filled in: such a catalog is rebuilt from
-- its packages and registration log instead (ADR 0002 §4, ADR 0009 §1).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM package) THEN
    RAISE EXCEPTION 'packages registered before migration 0006 have no thread index; rebuild'
      ' this catalog from its packages and registration log (ADR 0010)';
  END IF;
END
$$;

-- 1. A transform's upstream list is ordered (root ADR 0016 §4): latest_transform compares chains
-- flattened in consumed order, and lineage() reports each edge's place (LineageEdge.position).
ALTER TABLE transform_upstream ADD COLUMN position integer NOT NULL CHECK (position >= 0),
  ADD CONSTRAINT transform_upstream_position UNIQUE (tenant_id, transform_id, position);

-- 2. Every thread key a member or an Ambiguous candidate names, once. thread_id is
-- "sha256:" + hex(sha256(canonical JSON of {"key", "kind"})) (ADR 0003 §1.3), and key is that
-- canonical JSON, so equal ids mean equal keys.
CREATE TABLE thread (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  thread_id content_id NOT NULL,
  kind text NOT NULL CHECK (kind IN ('asset', 'configuration', 'document', 'machine', 'run',
                                     'sensor', 'site', 'software_version', 'stream')),
  key text NOT NULL CHECK (key LIKE '{%}'),  -- canonical JSON of the ThreadKey
  PRIMARY KEY (tenant_id, thread_id)
);

-- 3. One row per thread entry (ADR 0003 §2): a record in one registering package, the roles it
-- holds, and what ordering and lineage need, copied from its record row. world is the entry's
-- ThreadEntry.world as canonical JSON, its end restating the package field verbatim.
CREATE TABLE thread_member (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  thread_id content_id NOT NULL,
  package_id content_id NOT NULL,
  record_id record_id NOT NULL,
  kind text NOT NULL,
  registration_key bigint NOT NULL,
  roles text[] NOT NULL
    CHECK (cardinality(roles) >= 1 AND roles <@ ARRAY['cites', 'part_of', 'subject']),
  transform_id record_id NOT NULL,
  source_content_id content_id NOT NULL,
  world_clock record_id,
  world_first bigint,
  world_last bigint,
  world text NOT NULL CHECK (world LIKE '{%}'),
  PRIMARY KEY (tenant_id, thread_id, package_id, record_id),
  FOREIGN KEY (tenant_id, thread_id) REFERENCES thread (tenant_id, thread_id),
  FOREIGN KEY (tenant_id, kind, record_id, package_id)
    REFERENCES record (tenant_id, kind, record_id, package_id),
  FOREIGN KEY (tenant_id, package_id, registration_key)
    REFERENCES package (tenant_id, package_id, tx_seq),
  CHECK ((world_clock IS NULL) = (world_first IS NULL)),
  CHECK (world_last IS NULL OR world_first IS NOT NULL)
);
-- A thread at a catalog point: one index range scan (ADR 0005 §5's budget).
CREATE INDEX thread_member_by_thread ON thread_member (thread_id, registration_key);
-- threads_of(record id).
CREATE INDEX thread_member_by_record ON thread_member (record_id);

-- 4. A record whose Ambiguous field names this thread among its candidates (ADR 0003 §2): kept
-- discoverable, never an entry.
CREATE TABLE thread_unresolved (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  thread_id content_id NOT NULL,
  package_id content_id NOT NULL,
  record_id record_id NOT NULL,
  kind text NOT NULL,
  pointer text NOT NULL CHECK (pointer ~ '^(/([^~/]|~[01])*)+$'),
  registration_key bigint NOT NULL,
  PRIMARY KEY (tenant_id, thread_id, package_id, record_id, pointer),
  FOREIGN KEY (tenant_id, thread_id) REFERENCES thread (tenant_id, thread_id),
  FOREIGN KEY (tenant_id, kind, record_id, package_id)
    REFERENCES record (tenant_id, kind, record_id, package_id),
  FOREIGN KEY (tenant_id, package_id, registration_key)
    REFERENCES package (tenant_id, package_id, tx_seq)
);
CREATE INDEX thread_unresolved_by_thread ON thread_unresolved (thread_id, registration_key);
CREATE INDEX thread_unresolved_by_record ON thread_unresolved (record_id);

-- Append-only, like every table registration writes (ADR 0002 §6, ADR 0003 §5).
CREATE TRIGGER thread_append_only BEFORE UPDATE OR DELETE ON thread
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER thread_no_truncate BEFORE TRUNCATE ON thread
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER thread_member_append_only BEFORE UPDATE OR DELETE ON thread_member
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER thread_member_no_truncate BEFORE TRUNCATE ON thread_member
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER thread_unresolved_append_only BEFORE UPDATE OR DELETE ON thread_unresolved
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER thread_unresolved_no_truncate BEFORE TRUNCATE ON thread_unresolved
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
