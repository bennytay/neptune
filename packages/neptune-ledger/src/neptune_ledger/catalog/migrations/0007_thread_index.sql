-- 0007 the derived entity-thread index and ordered transform upstream (Ledger ADRs 0003, 0010).
--
-- Applied after 0006 in the same tenant schema, with search_path set to that schema alone.
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
    RAISE EXCEPTION 'packages registered before migration 0007 have no thread index; rebuild'
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

-- 5. One row per id an IdentityLink's right side states (package schema 3, root ADR 0050 §4):
-- one when Known, one per candidate when Ambiguous. A thread keyed by either id lists the link
-- as an edge (ADR 0003 §1.5); nothing is joined through it. left_id and right_id are canonical
-- JSON of {"namespace", "value"}, so equal text means equal ids (ADR 0010 §8).
CREATE TABLE thread_identity_link (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  package_id content_id NOT NULL,
  record_id record_id NOT NULL,
  kind text NOT NULL CHECK (kind = 'identity_link'),
  registration_key bigint NOT NULL,
  left_id text NOT NULL CHECK (left_id LIKE '{%}'),
  right_id text NOT NULL CHECK (right_id LIKE '{%}' AND right_id <> left_id),
  state text NOT NULL CHECK (state IN ('ambiguous', 'known')),
  assertion_kind text NOT NULL CHECK (assertion_kind IN ('observed', 'stated')),
  PRIMARY KEY (tenant_id, record_id, package_id, right_id),
  FOREIGN KEY (tenant_id, kind, record_id, package_id)
    REFERENCES record (tenant_id, kind, record_id, package_id),
  FOREIGN KEY (tenant_id, package_id, registration_key)
    REFERENCES package (tenant_id, package_id, tx_seq)
);
CREATE INDEX thread_identity_link_by_left ON thread_identity_link (left_id, registration_key);
CREATE INDEX thread_identity_link_by_right ON thread_identity_link (right_id, registration_key);

-- 6. A ClockMapping (package schema 3, root ADR 0050 §5) as the cross-clock merge reads it:
-- slope, offset, bound and the half-open source-clock window as canonical JSON, or why the merge
-- cannot use it (ADR 0010 §9). A thread merge names mappings by record id.
CREATE TABLE thread_clock_mapping (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  package_id content_id NOT NULL,
  record_id record_id NOT NULL,
  kind text NOT NULL CHECK (kind = 'clock_mapping'),
  registration_key bigint NOT NULL,
  source_clock record_id NOT NULL,
  target_clock record_id NOT NULL CHECK (target_clock <> source_clock),
  mapping text NOT NULL CHECK (mapping LIKE '{%}'),
  PRIMARY KEY (tenant_id, record_id, package_id),
  FOREIGN KEY (tenant_id, kind, record_id, package_id)
    REFERENCES record (tenant_id, kind, record_id, package_id),
  FOREIGN KEY (tenant_id, package_id, registration_key)
    REFERENCES package (tenant_id, package_id, tx_seq)
);

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
CREATE TRIGGER thread_identity_link_append_only BEFORE UPDATE OR DELETE ON thread_identity_link
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER thread_identity_link_no_truncate BEFORE TRUNCATE ON thread_identity_link
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER thread_clock_mapping_append_only BEFORE UPDATE OR DELETE ON thread_clock_mapping
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER thread_clock_mapping_no_truncate BEFORE TRUNCATE ON thread_clock_mapping
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
