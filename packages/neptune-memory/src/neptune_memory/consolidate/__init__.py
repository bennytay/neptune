"""Deterministic consolidation of Ledger records into claims (ADR 0003).

Rule: never imports ``derived/`` or any model / LLM client. Same Ledger content + consolidator
version + config gives byte-identical claims. Missing evidence stays explicit, never a fact.

``base`` holds the consolidator contract, claim stamping and ``rebuild``; ``identity`` holds
the identity policy (``same_as`` only on declared grounds, everything else a candidate) and
``identity_records`` parses the Ledger records it reads (ADR 0008); ``runs`` holds run threads
(nodes, membership, continuation, declared roles and intervals) and ``run_records`` parses its
records (ADR 0009); ``time`` holds the time-domain registry (clocks per machine, declared clock
mappings and chains of them, never estimated) and ``time_records`` parses what it reads (ADR 0011);
``episodes`` holds episodes from stated task evidence, bounded by stated instants, with
interventions and stops, and ``event_records`` parses the stated events both it and ``events``
read (ADR 0012); ``events`` holds events (nodes per stated record, registered kinds through
declared vendor mappings, co-occurrence that is never cause), and ``event_records`` also parses
its tables and config (ADR 0013); ``calibration`` holds calibration history per sensor, drift
between consecutive calibrations in declared units and ``calibrated_by``, and
``calibration_records`` parses what it reads (ADR 0014);
``coverage`` holds what each run recorded, its gaps, rates, integrity findings and sensor presence,
and ``coverage_records`` parses its records (ADR 0015).
"""
