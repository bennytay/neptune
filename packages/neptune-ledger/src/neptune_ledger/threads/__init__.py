"""Entity threads: ordered, provenance-linked views of one entity across packages (ADR 0003).

``membership`` computes, at registration, which threads each record of a package opens or joins
(the derived thread index, ADR 0010). ``order`` holds the pure world and transaction orders and
the current-view resolver, ``merge`` the caller-named cross-clock merge, and ``read`` answers
``thread`` and ``threads_of`` from the index at one catalog point.
"""
