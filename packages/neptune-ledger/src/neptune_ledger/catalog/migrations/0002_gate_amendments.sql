-- 0002 L1 gate amendments (Ledger ADR 0005): constant-time clock checks, record body digests,
-- location absences.
--
-- Applied after 0001 in the same tenant schema, with search_path set to that schema alone. 0001 is
-- never edited; everything below replaces or adds to it.

-- 1. The "never goes backwards" checks without full-table scans (ADR 0005 §1).
--
-- 0001's triggers scanned every registration_log and package row on each insert, so a
-- registration cost O(n) in the catalog's size. The clock already makes them redundant: tx_clock
-- only moves forward (its trigger), and a log entry must equal the clock's current tick. So every
-- earlier entry used a tick the clock has since left: a smaller tx_seq (the primary key forbids an
-- equal one) and a tx_time no later. Comparing with the one clock row gives the same guarantee in
-- O(1).
CREATE OR REPLACE FUNCTION registration_log_follows_clock() RETURNS trigger
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $$
DECLARE
  clock tx_clock%ROWTYPE;
BEGIN
  SELECT * INTO clock FROM tx_clock;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'tenant schema has no transaction clock row';
  END IF;
  IF NEW.tx_seq < clock.last_seq OR NEW.tx_time COLLATE "C" < clock.last_time COLLATE "C" THEN
    RAISE EXCEPTION 'transaction time never goes backwards: (%, %) follows a later registration',
      NEW.tx_seq, NEW.tx_time;
  END IF;
  IF NEW.tx_seq <> clock.last_seq OR NEW.tx_time IS DISTINCT FROM clock.last_time THEN
    RAISE EXCEPTION 'registration (%, %) is not the tick just allocated', NEW.tx_seq, NEW.tx_time;
  END IF;
  RETURN NEW;
END
$$;

-- A package row is written while its log entry's tick is still the clock's last one: in the same
-- transaction on a live registration, before the next replay_tx on a rebuild. Any later package
-- needs a later log entry, which needs a later tick, so a package can never follow a later one.
-- The log lookup names tenant_id so it uses the (tenant_id, package_id) unique index.
CREATE OR REPLACE FUNCTION package_from_log() RETURNS trigger
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $$
DECLARE
  entry registration_log%ROWTYPE;
BEGIN
  SELECT * INTO entry FROM registration_log
   WHERE tenant_id = NEW.tenant_id AND package_id = NEW.package_id;
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
  IF NOT EXISTS (SELECT 1 FROM tx_clock WHERE last_seq = entry.tx_seq) THEN
    RAISE EXCEPTION 'transaction time never goes backwards: package % follows a later one',
      NEW.package_id;
  END IF;
  RETURN NEW;
END
$$;

-- 2. Record body digests (ADR 0005 §2).
--
-- A tier-2 record id covers the record's evidence and transform, not its body, so a hostile but
-- self-consistent package can bring an existing record id with another body. body_digest is the
-- sha256 of the record's canonical JSON line in its table (without the newline). One (kind,
-- record_id) has one digest in a tenant: registration refuses a package that brings another one
-- (a conflicting_id finding), and this trigger refuses the row if anything gets that far.
-- NOT NULL without a default: 0002 must be applied before the first registration, which it is,
-- because apply_migrations runs every pending migration in one transaction and no registration
-- implementation predates it.
ALTER TABLE record ADD COLUMN body_digest content_id NOT NULL;

CREATE FUNCTION record_body_agrees() RETURNS trigger
LANGUAGE plpgsql
SET search_path FROM CURRENT
AS $$
DECLARE
  stored text;
BEGIN
  SELECT body_digest INTO stored FROM record
   WHERE kind = NEW.kind AND record_id = NEW.record_id AND body_digest <> NEW.body_digest
   LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION '% % is catalogued with body %, arrived with body %',
      NEW.kind, NEW.record_id, stored, NEW.body_digest;
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER record_body_agrees BEFORE INSERT ON record
  FOR EACH ROW EXECUTE FUNCTION record_body_agrees();

-- 3. Location absences (ADR 0005 §3): every source_absence record per package, with the location
-- as stated and the revisions it supersedes, so resolve() can leave out a location that the same
-- package says no longer holds the bytes. Like source_location, nothing is ever removed.
CREATE TABLE location_absence (
  tenant_id text NOT NULL REFERENCES tenant (tenant_id),
  absence_id record_id NOT NULL,
  package_id content_id NOT NULL,
  location text NOT NULL CHECK (location LIKE '{%}'),  -- canonical JSON, as the record states it
  supersedes text[] NOT NULL DEFAULT '{}',  -- record ids of the revisions or absences it supersedes
  PRIMARY KEY (tenant_id, absence_id, package_id),
  FOREIGN KEY (tenant_id, package_id) REFERENCES package (tenant_id, package_id)
);
CREATE INDEX location_absence_by_package ON location_absence (package_id);

CREATE TRIGGER location_absence_append_only BEFORE UPDATE OR DELETE ON location_absence
  FOR EACH ROW EXECUTE FUNCTION refuse_change();
CREATE TRIGGER location_absence_no_truncate BEFORE TRUNCATE ON location_absence
  FOR EACH STATEMENT EXECUTE FUNCTION refuse_change();
