-- 0001 catalog: package registry, record, source and transform indexes, clocks (ADR 0002).
--
-- Applied by neptune_ledger.catalog.migrate inside one tenant's schema, with search_path set to
-- that schema alone, so every name below is created there. Nothing here names a schema, and
-- nothing references a package file: packages are immutable and external (ADR 0002 §3).
--
-- Conventions checked by tests/test_catalog_schema.py:
-- * every table has tenant_id, pinned by a foreign key to the schema's single tenant row;
-- * transaction time is tx_seq + tx_time (the Ledger's clock); world time is world_* ticks on a
--   named clock; no column has a timestamp, date or interval type;
-- * no column stores interpretation; assertion_kind admits only the canonical observed/stated;
-- * a NULL index column means "not Known in the record", never "absent": the package holds the
--   field's missingness state.

-- Identifier shapes, as the canonical model renders them (root ADRs 0003, 0009).
CREATE DOMAIN content_id AS text CHECK (VALUE ~ '^sha256:[0-9a-f]{64}$');
CREATE DOMAIN record_id AS text CHECK (VALUE ~ '^rec:sha256:[0-9a-f]{64}$');
-- RFC 3339 UTC with exactly six fractional digits, so text order is time order.
CREATE DOMAIN tx_time AS text COLLATE "C"
  CHECK (VALUE ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$');

-- The schema's one tenant. Every other table's tenant_id references it, so no row of another
-- tenant can exist in this schema (ADR 0002 §2).
CREATE TABLE tenant (
  tenant_id text PRIMARY KEY CHECK (tenant_id ~ '^[a-z][a-z0-9_]{0,47}$'),
  singleton boolean NOT NULL DEFAULT true UNIQUE CHECK (singleton)
);

CREATE TABLE schema_migration (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  version integer NOT NULL CHECK (version >= 1),
  name text NOT NULL,
  sha256 content_id NOT NULL,
  PRIMARY KEY (tenant_id, version)
);

-- The Ledger's transaction clock (ADR 0002 §4). One row; last_seq is strictly increasing and
-- last_time never decreases, whatever the host's wall clock does.
CREATE TABLE tx_clock (
  tenant_id text PRIMARY KEY REFERENCES tenant (tenant_id),
  last_seq bigint NOT NULL CHECK (last_seq >= 0),
  last_time tx_time,
  CHECK ((last_seq = 0) = (last_time IS NULL))
);

-- The clock only moves forward: every UPDATE must raise last_seq and keep last_time or raise it;
-- DELETE and TRUNCATE are refused (§4).
CREATE FUNCTION tx_clock_forward() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'UPDATE'
     AND NEW.tenant_id = OLD.tenant_id
     AND NEW.last_seq > OLD.last_seq
     AND NEW.last_time IS NOT NULL
     AND (OLD.last_time IS NULL OR NEW.last_time COLLATE "C" >= OLD.last_time COLLATE "C") THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'the transaction clock only moves forward';
END
$$;
CREATE TRIGGER tx_clock_forward BEFORE UPDATE OR DELETE ON tx_clock
  FOR EACH ROW EXECUTE FUNCTION tx_clock_forward();
CREATE TRIGGER tx_clock_no_truncate BEFORE TRUNCATE ON tx_clock
  FOR EACH STATEMENT EXECUTE FUNCTION tx_clock_forward();

-- Allocate the next transaction tick. Locks the clock row, so registrations in one tenant are
-- serialised; the lock is held until the calling transaction ends.
CREATE FUNCTION next_tx(OUT tx_seq bigint, OUT tx_time text)
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $$
DECLARE
  wall text := to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
BEGIN
  UPDATE tx_clock
     SET last_seq = last_seq + 1,
         last_time = GREATEST(COALESCE(last_time, wall) COLLATE "C", wall COLLATE "C")
  RETURNING last_seq, last_time INTO tx_seq, tx_time;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'tenant schema has no transaction clock row';
  END IF;
END
$$;

-- Advance the clock to a tick from the registration log, on a rebuild (ADR 0002 §4). The tick
-- must come after the clock's last one, so a replay in tx_seq order reproduces the original
-- transaction times and the next live tick continues after them.
CREATE FUNCTION replay_tx(p_seq bigint, p_time text) RETURNS void
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $$
BEGIN
  UPDATE tx_clock
     SET last_seq = p_seq, last_time = p_time
   WHERE p_seq > last_seq
     AND (last_time IS NULL OR p_time COLLATE "C" >= last_time COLLATE "C");
  IF NOT FOUND THEN
    RAISE EXCEPTION 'tick (%, %) does not follow the transaction clock', p_seq, p_time;
  END IF;
END
$$;

-- Refuse UPDATE, DELETE and TRUNCATE: registering a package only adds rows (§6; ADR 0003 §5).
CREATE FUNCTION refuse_change() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'catalog table % is append-only', TG_TABLE_NAME;
END
$$;

-- The Ledger registration log (§4; ADR 0003 §8): one row per registration, in tx_seq order,
-- append-only. With the packages and the same Ledger version it is the whole rebuild input, so it
-- holds everything a registration knows that no package states.
CREATE TABLE registration_log (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  tx_seq bigint NOT NULL CHECK (tx_seq >= 1),
  tx_time tx_time NOT NULL,
  package_id content_id NOT NULL,       -- sha256 of the package's manifest.json bytes
  root_locator text NOT NULL CHECK (root_locator <> ''),  -- where it was registered from
  ledger_version text NOT NULL CHECK (ledger_version ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
  PRIMARY KEY (tenant_id, tx_seq),
  UNIQUE (tenant_id, package_id),
  UNIQUE (tenant_id, package_id, tx_seq)  -- target of package's foreign key
);

-- A log entry uses the tick just allocated (next_tx or replay_tx) and never goes back.
CREATE FUNCTION registration_log_follows_clock() RETURNS trigger
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM registration_log
              WHERE tx_seq >= NEW.tx_seq OR tx_time COLLATE "C" > NEW.tx_time COLLATE "C") THEN
    RAISE EXCEPTION 'transaction time never goes backwards: (%, %) follows a later registration',
      NEW.tx_seq, NEW.tx_time;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM tx_clock
                  WHERE last_seq = NEW.tx_seq AND last_time = NEW.tx_time) THEN
    RAISE EXCEPTION 'registration (%, %) is not the tick just allocated', NEW.tx_seq, NEW.tx_time;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER registration_log_follows_clock BEFORE INSERT ON registration_log
  FOR EACH ROW EXECUTE FUNCTION registration_log_follows_clock();

-- One row per registered package. Re-registering the same package id changes nothing (§6). The
-- transaction and registration columns are copied from the package's log entry.
CREATE TABLE package (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  package_id content_id NOT NULL,
  schema_version integer NOT NULL CHECK (schema_version >= 1),
  receipt_id record_id NOT NULL,        -- manifest.receipt
  root_locator text NOT NULL,
  ledger_version text NOT NULL,
  tx_seq bigint NOT NULL,
  tx_time tx_time NOT NULL,
  PRIMARY KEY (tenant_id, package_id),
  UNIQUE (tenant_id, tx_seq),
  UNIQUE (tenant_id, package_id, tx_seq),  -- target of record.registration_key
  FOREIGN KEY (tenant_id, package_id, tx_seq)
    REFERENCES registration_log (tenant_id, package_id, tx_seq)
);
CREATE INDEX package_by_tx_time ON package (tx_time, tx_seq);
CREATE INDEX package_by_receipt ON package (receipt_id);

-- Fill a package's log columns from its log entry, refuse any that disagree, and refuse a package
-- whose registration is older than one already in the catalog.
CREATE FUNCTION package_from_log() RETURNS trigger
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $$
DECLARE
  entry registration_log%ROWTYPE;
BEGIN
  SELECT * INTO entry FROM registration_log WHERE package_id = NEW.package_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'package % is not in the registration log', NEW.package_id;
  END IF;
  IF (NEW.tx_seq IS NOT NULL AND NEW.tx_seq <> entry.tx_seq)
     OR (NEW.tx_time IS NOT NULL AND NEW.tx_time <> entry.tx_time)
     OR (NEW.root_locator IS NOT NULL AND NEW.root_locator <> entry.root_locator)
     OR (NEW.ledger_version IS NOT NULL AND NEW.ledger_version <> entry.ledger_version) THEN
    RAISE EXCEPTION 'package % disagrees with its registration log entry', NEW.package_id;
  END IF;
  NEW.tx_seq := entry.tx_seq;
  NEW.tx_time := entry.tx_time;
  NEW.root_locator := entry.root_locator;
  NEW.ledger_version := entry.ledger_version;
  IF EXISTS (SELECT 1 FROM package
              WHERE tx_seq >= NEW.tx_seq OR tx_time COLLATE "C" > NEW.tx_time COLLATE "C") THEN
    RAISE EXCEPTION 'transaction time never goes backwards: package % follows a later one',
      NEW.package_id;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER package_from_log BEFORE INSERT ON package
  FOR EACH ROW EXECUTE FUNCTION package_from_log();

-- Source bytes by content id: one row however many packages cite them.
CREATE TABLE source (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  content_id content_id NOT NULL,
  size bigint NOT NULL CHECK (size >= 0),
  PRIMARY KEY (tenant_id, content_id)
);

-- Which packages cite which sources, and how each package reaches the bytes (manifest.sources).
CREATE TABLE package_source (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  package_id content_id NOT NULL,
  content_id content_id NOT NULL,
  storage text NOT NULL CHECK (storage IN ('referenced', 'materialised')),
  PRIMARY KEY (tenant_id, package_id, content_id),
  FOREIGN KEY (tenant_id, package_id) REFERENCES package (tenant_id, package_id),
  FOREIGN KEY (tenant_id, content_id) REFERENCES source (tenant_id, content_id)
);
CREATE INDEX package_source_by_content ON package_source (content_id);

-- Every location a source was seen at: one row per source_revision record per package.
CREATE TABLE source_location (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  revision_id record_id NOT NULL,
  package_id content_id NOT NULL,
  content_id content_id NOT NULL,
  location text NOT NULL CHECK (location LIKE '{%}'),  -- canonical JSON, as the record states it
  supersedes text[] NOT NULL DEFAULT '{}',  -- record ids of the revisions this one supersedes
  PRIMARY KEY (tenant_id, revision_id, package_id),
  FOREIGN KEY (tenant_id, package_id) REFERENCES package (tenant_id, package_id),
  FOREIGN KEY (tenant_id, content_id) REFERENCES source (tenant_id, content_id)
);
CREATE INDEX source_location_by_content ON source_location (content_id);
CREATE INDEX source_location_by_package ON source_location (package_id);

-- Transforms by id: the nodes of the lineage DAG (transform_record).
CREATE TABLE transform (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  transform_id record_id NOT NULL,
  adapter_id text NOT NULL CHECK (adapter_id <> ''),
  adapter_version text NOT NULL CHECK (adapter_version <> ''),
  config_hash content_id NOT NULL,
  libraries text NOT NULL CHECK (libraries LIKE '{%}'),  -- canonical JSON text
  PRIMARY KEY (tenant_id, transform_id)
);
CREATE INDEX transform_by_adapter ON transform (adapter_id, adapter_version);

-- The DAG's edges, as declared. upstream_id has no foreign key: a declared edge to a transform
-- not (yet) registered is kept as stated, never dropped (ADR 0002 §5).
CREATE TABLE transform_upstream (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  transform_id record_id NOT NULL,
  upstream_id record_id NOT NULL CHECK (upstream_id <> transform_id),
  PRIMARY KEY (tenant_id, transform_id, upstream_id),
  FOREIGN KEY (tenant_id, transform_id) REFERENCES transform (tenant_id, transform_id)
);
CREATE INDEX transform_upstream_by_upstream ON transform_upstream (upstream_id);

-- Every TimestampDomain seen, by package and scope. The domain's other fields stay in the package.
CREATE TABLE clock (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  clock_id record_id NOT NULL,          -- the timestamp_domain record id
  package_id content_id NOT NULL,
  field text NOT NULL,
  scope text[] NOT NULL,
  PRIMARY KEY (tenant_id, clock_id, package_id),
  FOREIGN KEY (tenant_id, package_id) REFERENCES package (tenant_id, package_id)
);
CREATE INDEX clock_by_package ON clock (package_id);
CREATE INDEX clock_by_scope ON clock USING gin (scope);

-- One row per canonical record per package that holds it, partitioned by kind (ADR 0002 §5).
-- record_id is the table key of neptune.model.kinds.record_key: a record id, or a content id for
-- source_artifact. line is the record's 1-based line in the package's records/<kind>.jsonl.
CREATE TABLE record (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  kind text NOT NULL,
  record_id text NOT NULL,
  package_id content_id NOT NULL,
  -- The registering package's tx_seq: the registration key ADR 0003 §3 breaks ties with.
  registration_key bigint NOT NULL,
  line integer NOT NULL CHECK (line >= 1),
  schema_version integer NOT NULL CHECK (schema_version >= 1),
  -- Provenance summary: the record-level provenance (a finding's subject and transform). The
  -- source and locator are the record's EvidenceRef, the evidence anchor of ADR 0003 §1.
  source_content_id content_id,
  source_locator text CHECK (source_locator LIKE '[%]'),  -- canonical JSON text, never jsonb
  transform_id record_id,
  assertion_kind text CHECK (assertion_kind IN ('observed', 'stated')),
  -- World time of the record as ADR 0003 §3 defines it: start and end ticks on the start's clock,
  -- exactly as stated; world_last is NULL when the end is open.
  world_clock record_id,
  world_first bigint,
  world_last bigint,
  -- JSON pointers, into the record, of every field whose state is Ambiguous.
  ambiguous_pointers text[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (tenant_id, kind, record_id, package_id),
  UNIQUE (tenant_id, kind, package_id, line),
  FOREIGN KEY (tenant_id, package_id, registration_key)
    REFERENCES package (tenant_id, package_id, tx_seq),
  CHECK ((source_content_id IS NULL) = (source_locator IS NULL)),
  CHECK (CASE WHEN kind = 'source_artifact' THEN record_id ~ '^sha256:[0-9a-f]{64}$'
              ELSE record_id ~ '^rec:sha256:[0-9a-f]{64}$' END),
  CHECK ((world_clock IS NULL) = (world_first IS NULL)),
  CHECK (world_last IS NULL OR world_first IS NOT NULL)
) PARTITION BY LIST (kind);

-- One partition per record kind of package schema 1 (neptune.model.kinds.RECORD_KINDS). There is
-- no default partition: a kind this list does not name is refused, never filed silently.
CREATE TABLE record_source_artifact PARTITION OF record FOR VALUES IN ('source_artifact');
CREATE TABLE record_source_revision PARTITION OF record FOR VALUES IN ('source_revision');
CREATE TABLE record_source_absence PARTITION OF record FOR VALUES IN ('source_absence');
CREATE TABLE record_transform_record PARTITION OF record FOR VALUES IN ('transform_record');
CREATE TABLE record_ingest_finding PARTITION OF record FOR VALUES IN ('ingest_finding');
CREATE TABLE record_timestamp_domain PARTITION OF record FOR VALUES IN ('timestamp_domain');
CREATE TABLE record_frame_graph PARTITION OF record FOR VALUES IN ('frame_graph');
CREATE TABLE record_frame PARTITION OF record FOR VALUES IN ('frame');
CREATE TABLE record_frame_transform PARTITION OF record FOR VALUES IN ('frame_transform');
CREATE TABLE record_run PARTITION OF record FOR VALUES IN ('run');
CREATE TABLE record_stream PARTITION OF record FOR VALUES IN ('stream');
CREATE TABLE record_machine PARTITION OF record FOR VALUES IN ('machine');
CREATE TABLE record_hardware_configuration PARTITION OF record
  FOR VALUES IN ('hardware_configuration');
CREATE TABLE record_hardware_component PARTITION OF record FOR VALUES IN ('hardware_component');
CREATE TABLE record_software_configuration PARTITION OF record
  FOR VALUES IN ('software_configuration');
CREATE TABLE record_calibration PARTITION OF record FOR VALUES IN ('calibration');
CREATE TABLE record_site PARTITION OF record FOR VALUES IN ('site');
CREATE TABLE record_asset PARTITION OF record FOR VALUES IN ('asset');
CREATE TABLE record_spatial_artifact PARTITION OF record FOR VALUES IN ('spatial_artifact');
CREATE TABLE record_image PARTITION OF record FOR VALUES IN ('image');
CREATE TABLE record_video PARTITION OF record FOR VALUES IN ('video');
CREATE TABLE record_document_record PARTITION OF record FOR VALUES IN ('document_record');
CREATE TABLE record_document_block PARTITION OF record FOR VALUES IN ('document_block');
CREATE TABLE record_structured_table PARTITION OF record FOR VALUES IN ('structured_table');
CREATE TABLE record_structured_record PARTITION OF record FOR VALUES IN ('structured_record');

-- Lookups the entity-thread resolver and the time-window queries need (ADR 0002 §7).
CREATE INDEX record_by_id ON record (record_id);
CREATE INDEX record_by_package ON record (package_id, kind);
CREATE INDEX record_by_transform ON record (transform_id) WHERE transform_id IS NOT NULL;
-- Evidence anchors and lineage sets: (source content id, kind), then siblings by locator. The
-- locator is indexed by digest, so a long one cannot exceed the B-tree entry limit; queries
-- compare source_locator itself.
CREATE INDEX record_by_evidence ON record (source_content_id, kind, md5(source_locator))
  WHERE source_content_id IS NOT NULL;
-- Per-clock order: (clock, start, end, registration key), as ADR 0003 §3 sorts a partition.
CREATE INDEX record_by_world_time
  ON record (world_clock, world_first, world_last, registration_key)
  WHERE world_clock IS NOT NULL;
CREATE INDEX record_by_registration ON record (registration_key, record_id, package_id);
CREATE INDEX record_by_ambiguous ON record USING gin (ambiguous_pointers)
  WHERE ambiguous_pointers <> '{}';

-- Every Known logical id a record states, at the JSON pointer it is stated at. Grouping records
-- by these ids is the entity-thread resolver's decision, not this table's (ADR 0002 §7).
CREATE TABLE record_logical_id (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  kind text NOT NULL,
  record_id text NOT NULL,
  package_id content_id NOT NULL,
  pointer text NOT NULL CHECK (pointer ~ '^(/([^~/]|~[01])*)+$'),
  namespace text NOT NULL,
  value text NOT NULL,
  PRIMARY KEY (tenant_id, kind, record_id, package_id, pointer),
  FOREIGN KEY (tenant_id, kind, record_id, package_id)
    REFERENCES record (tenant_id, kind, record_id, package_id)
);
CREATE INDEX record_logical_id_by_value ON record_logical_id (namespace, value, kind);

-- Append-only, every table registration writes.
CREATE TRIGGER registration_log_append_only BEFORE UPDATE OR DELETE ON registration_log
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER registration_log_no_truncate BEFORE TRUNCATE ON registration_log
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER package_append_only BEFORE UPDATE OR DELETE ON package
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER source_append_only BEFORE UPDATE OR DELETE ON source
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER package_source_append_only BEFORE UPDATE OR DELETE ON package_source
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER source_location_append_only BEFORE UPDATE OR DELETE ON source_location
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER transform_append_only BEFORE UPDATE OR DELETE ON transform
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER transform_upstream_append_only BEFORE UPDATE OR DELETE ON transform_upstream
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER clock_append_only BEFORE UPDATE OR DELETE ON clock
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER record_append_only BEFORE UPDATE OR DELETE ON record
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER record_logical_id_append_only BEFORE UPDATE OR DELETE ON record_logical_id
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER package_no_truncate BEFORE TRUNCATE ON package
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER source_no_truncate BEFORE TRUNCATE ON source
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER package_source_no_truncate BEFORE TRUNCATE ON package_source
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER source_location_no_truncate BEFORE TRUNCATE ON source_location
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER transform_no_truncate BEFORE TRUNCATE ON transform
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER transform_upstream_no_truncate BEFORE TRUNCATE ON transform_upstream
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER clock_no_truncate BEFORE TRUNCATE ON clock
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER record_no_truncate BEFORE TRUNCATE ON record
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER record_logical_id_no_truncate BEFORE TRUNCATE ON record_logical_id
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
