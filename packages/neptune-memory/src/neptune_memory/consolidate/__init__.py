"""Deterministic consolidation of Ledger records into claims (ADR 0003).

Rule: never imports ``derived/`` or any model / LLM client. Same Ledger content + consolidator
version + config gives byte-identical claims. Missing evidence stays explicit, never a fact.

``base`` holds the consolidator contract, claim stamping and ``rebuild``; ``identity`` holds
the identity policy (``same_as`` only on declared grounds, everything else a candidate) and
``identity_records`` parses the Ledger records it reads (ADR 0008); ``time`` holds the time-domain
registry (clocks per machine, declared clock mappings and chains of them, never estimated) and
``time_records`` parses what it reads (ADR 0011).
"""
