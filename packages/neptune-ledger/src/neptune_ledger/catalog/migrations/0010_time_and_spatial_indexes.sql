-- 0010 the derived time and spatial indexes (Ledger ADR 0015).
--
-- Applied in the same tenant schema, with search_path set to that schema alone. Both tables are
-- derived: registration computes every row from the verified package (its record lines and its
-- series files) and writes it in the registration transaction, so a replay of the registration
-- log rebuilds them byte for byte (ADR 0012). Nothing here converts a tick, a unit or a frame.

-- 0. Packages registered before this migration have no index rows. Every table here is
-- append-only, so they cannot be filled in: such a catalog is rebuilt from its packages and
-- registration log instead (ADR 0002 §4, ADR 0012).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM package) THEN
    RAISE EXCEPTION 'packages registered before migration 0010 have no time or spatial index;'
      ' rebuild this catalog from its packages and registration log (ADR 0015)';
  END IF;
END
$$;

-- 1. The R-tree key of one clock or one spatial reference: the first 52 bits of the sha256 of
-- its text, a whole number a float8 holds exactly. Each GiST key below puts it on an axis of its
-- own, so an index search stays inside one clock's or one reference's entries; two texts sharing a
-- key only share a subtree, since every query also compares the text itself.
CREATE FUNCTION index_key(t text) RETURNS double precision
  LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
  RETURN ('x' || substr(encode(sha256(convert_to(t, 'UTF8')), 'hex'), 1, 13))::bit(52)::bigint
    ::double precision;

-- 2. One row per interval on one clock (ADR 0015 §2). subject 'record' is a record's world time
-- (ADR 0003 §3, as record.world_* holds it): last_tick NULL when the end is open. subject 'series'
-- is the least and greatest known tick of one stream's series file on one of its clocks, with
-- how many rows have a known tick there and how many do not. Ticks are as stated, never
-- converted; intervals on two clocks are never compared.
CREATE TABLE time_interval (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  subject text NOT NULL CHECK (subject IN ('record', 'series')),
  kind text NOT NULL,
  record_id record_id NOT NULL,
  package_id content_id NOT NULL,
  clock record_id NOT NULL,
  first_tick bigint NOT NULL,
  last_tick bigint,
  rows_known bigint,
  rows_unknown bigint,
  registration_key bigint NOT NULL,
  -- The R-tree key: x is the clock's index_key, y the stated extent [first, last or first] as
  -- float8, ends ordered so a stated last < first still has one. Rounding to float8 never reverses
  -- an order, so the key holds every interval the exact test can keep; the bigint ticks decide.
  span box NOT NULL GENERATED ALWAYS AS (
    box(point(index_key(clock), least(first_tick, coalesce(last_tick, first_tick))::float8),
        point(index_key(clock), greatest(first_tick, coalesce(last_tick, first_tick))::float8))
  ) STORED,
  PRIMARY KEY (tenant_id, subject, record_id, package_id, clock),
  FOREIGN KEY (tenant_id, kind, record_id, package_id)
    REFERENCES record (tenant_id, kind, record_id, package_id),
  FOREIGN KEY (tenant_id, package_id, registration_key)
    REFERENCES package (tenant_id, package_id, tx_seq),
  CHECK ((subject = 'series') = (rows_known IS NOT NULL)),
  CHECK ((rows_known IS NULL) = (rows_unknown IS NULL)),
  CHECK (subject = 'record' OR (kind = 'stream' AND last_tick IS NOT NULL
                                AND last_tick >= first_tick AND rows_known >= 1
                                AND rows_unknown >= 0))
);
-- "What exists on clock C between t1 and t2": one R-tree search inside clock C's entries.
CREATE INDEX time_interval_by_span ON time_interval USING gist (span);
CREATE INDEX time_interval_by_record ON time_interval (record_id);

-- 3. One row per spatial reference a record states (ADR 0015 §4): a FrameRef (reference_kind
-- 'frame', reference the canonical JSON of {frame_graph_id, frame_id}) or a CRS ('crs', of
-- {authority, code}), at JSON pointer `pointer` of the record. Where the record also states
-- coordinates in that reference, extent_pointer names them and min_* / max_* hold them as
-- declared, in `unit` (NULL when the unit is not Known). There is no default world frame: a row
-- exists only for a reference the record states as Known.
CREATE TABLE spatial_extent (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  kind text NOT NULL,
  record_id record_id NOT NULL,
  package_id content_id NOT NULL,
  pointer text NOT NULL CHECK (pointer ~ '^(/([^~/]|~[01])*)+$'),
  registration_key bigint NOT NULL,
  reference_kind text NOT NULL CHECK (reference_kind IN ('crs', 'frame')),
  reference text NOT NULL CHECK (reference LIKE '{%}'),
  extent_pointer text CHECK (extent_pointer ~ '^(/([^~/]|~[01])*)+$'),
  dims smallint CHECK (dims IN (2, 3)),
  unit text,
  min_x double precision,
  min_y double precision,
  min_z double precision,
  max_x double precision,
  max_y double precision,
  max_z double precision,
  -- The R-tree keys (one GiST over both): the reference and unit as a point on an axis of their
  -- own, then the extent's x and y. z is compared on the rows the search returns.
  scope box NOT NULL GENERATED ALWAYS AS (
    box(point(index_key(reference_kind || ' ' || reference || ' ' || coalesce(unit, '')), 0),
        point(index_key(reference_kind || ' ' || reference || ' ' || coalesce(unit, '')), 0))
  ) STORED,
  xy box GENERATED ALWAYS AS (box(point(min_x, min_y), point(max_x, max_y))) STORED,
  PRIMARY KEY (tenant_id, record_id, package_id, pointer),
  FOREIGN KEY (tenant_id, kind, record_id, package_id)
    REFERENCES record (tenant_id, kind, record_id, package_id),
  FOREIGN KEY (tenant_id, package_id, registration_key)
    REFERENCES package (tenant_id, package_id, tx_seq),
  CHECK ((extent_pointer IS NULL) = (dims IS NULL)),
  CHECK (unit IS NULL OR dims IS NOT NULL),
  CHECK ((dims IS NULL) = (min_x IS NULL) AND (dims IS NULL) = (max_x IS NULL)
         AND (dims IS NULL) = (min_y IS NULL) AND (dims IS NULL) = (max_y IS NULL)),
  CHECK ((dims IS NOT DISTINCT FROM 3) = (min_z IS NOT NULL)
         AND (min_z IS NULL) = (max_z IS NULL)),
  CHECK (min_x <= max_x AND min_y <= max_y AND (min_z IS NULL OR min_z <= max_z))
);
-- A query names its reference and unit: the R-tree over that reference's extents in that unit,
-- and the reference's members for those it cannot compare.
CREATE INDEX spatial_extent_by_scope ON spatial_extent USING gist (scope, xy);
CREATE INDEX spatial_extent_by_reference ON spatial_extent (reference_kind, reference, unit);
CREATE INDEX spatial_extent_by_record ON spatial_extent (record_id);

-- Append-only, like every table registration writes (ADR 0002 §6).
CREATE TRIGGER time_interval_append_only BEFORE UPDATE OR DELETE ON time_interval
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER time_interval_no_truncate BEFORE TRUNCATE ON time_interval
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
CREATE TRIGGER spatial_extent_append_only BEFORE UPDATE OR DELETE ON spatial_extent
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER spatial_extent_no_truncate BEFORE TRUNCATE ON spatial_extent
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
