"""Persistence of the claim graph (planned).

Rule: packages are read only through ``neptune_memory.ledger.LedgerReader``; this subpackage never
opens package files and never imports ``neptune.store``.
"""
