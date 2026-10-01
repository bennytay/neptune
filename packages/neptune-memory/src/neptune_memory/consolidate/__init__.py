"""Deterministic consolidation of Ledger records into claims (planned).

Rule: never imports ``derived/`` or any model / LLM client. Same Ledger content + consolidator
version + config gives byte-identical claims. Missing evidence stays explicit, never a fact.
"""
