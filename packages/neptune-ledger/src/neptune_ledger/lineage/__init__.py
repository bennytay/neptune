"""Parser and adapter lineage: the transform DAG behind a record and its lineage siblings.

``graph`` answers ``lineage(record_id)`` and gives the current-view resolver each transform's
chain (ADR 0003 §4). Which transform is current is never decided here; that is ``thread`` with
an explicit preference.
"""
