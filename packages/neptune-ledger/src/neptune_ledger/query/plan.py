"""The record stage's plan: which index drives it, and the one statement it runs (ADR 0016 §2).

The rule is fixed, so the plan, and the explain output that names it, is a function of the spec
alone. The narrowest filter the spec names drives a ``MATERIALIZED`` candidate set: a thread's
members, then the window clock's interval R-tree, then the frame's extent R-tree, then the named
packages. Without any of them the record primary key is scanned in key order. Every filter is
then applied exactly to the ``record`` row, and rows come back in ``(kind, record_id,
package_id)`` order, one batch at a time from a server-side cursor.
"""

from dataclasses import dataclass
from typing import Any, Final, Literal

from neptune_ledger.api.types import PlanStep, QuerySpec
from neptune_ledger.lake.space_index import reference_text

Driver = Literal["thread", "window", "frame", "packages", "kinds"]

# The QueryRow columns, in QUERY_RESULT_SCHEMA order, from the record row.
ROW: Final = (
    "r.kind, r.record_id, r.package_id, r.line, r.registration_key, r.transform_id,"
    " r.source_content_id, r.source_locator, r.assertion_kind, r.world_clock, r.world_first,"
    " r.world_last"
)
_WINDOW: Final = (
    "{t}.world_clock = %(clock)s AND {t}.world_first <= %(last)s"
    " AND COALESCE({t}.world_last, {t}.world_first) >= %(first)s"
)
# ADR 0015 §5's placed rule on a stored extent; the R-tree, where it drives, only finds candidates.
_BOX: Final = (
    "s.reference_kind = %(reference_kind)s AND s.reference = %(reference)s AND s.unit = %(unit)s"
    " AND s.registration_key <= %(as_of)s"
    " AND s.min_x <= %(x1)s AND s.max_x >= %(x0)s AND s.min_y <= %(y1)s AND s.max_y >= %(y0)s"
)
_BOX_3D: Final = " AND s.dims = 3 AND s.min_z <= %(z1)s AND s.max_z >= %(z0)s"
# A series join selects streams by their series files' intervals on the window's clock (ADR 0015
# §2): the ticks the file holds there, not the stream record's stated world time.
_SERIES_WINDOW: Final = (
    "EXISTS (SELECT 1 FROM time_interval i WHERE i.tenant_id = r.tenant_id"
    " AND i.subject = 'series' AND i.record_id = r.record_id AND i.package_id = r.package_id"
    " AND i.clock = %(clock)s AND i.first_tick <= %(last)s AND i.last_tick >= %(first)s)"
)
_DRIVERS: Final[dict[Driver, str]] = {
    "thread": "thread_member by (thread_id, registration_key); members carry their world time",
    "window": "time_interval R-tree on the window's clock (ADR 0015 §6): record intervals, or"
    " series-file intervals for a series join",
    "frame": "spatial_extent R-tree on the frame's reference and unit (ADR 0015 §6)",
    "packages": "record_by_package (package_id, kind)",
    "kinds": "record primary key (tenant_id, kind, record_id, package_id) in key order",
}


@dataclass(frozen=True)
class RecordPlan:
    """The record stage: its driver, its statement and the parameters it binds.

    ``statement`` takes ``%(after_kind)s``, ``%(after_record)s`` and ``%(after_package)s`` (the
    keyset cursor; empty strings sort before every key) and ``%(limit)s`` (NULL for none)."""

    driver: Driver
    statement: str
    params: dict[str, Any]

    def step(self) -> PlanStep:
        detail = f"candidates from {_DRIVERS[self.driver]}; then every filter on record: "
        return PlanStep("postgres", f"candidates.{self.driver}", detail + self.statement)


def driver(spec: QuerySpec) -> Driver:
    if spec.thread_id is not None:
        return "thread"
    if spec.window is not None:
        return "window"
    if spec.frame is not None:
        return "frame"
    if spec.packages is not None:
        return "packages"
    return "kinds"


def plan_records(spec: QuerySpec, tenant: str, as_of: int) -> RecordPlan:
    """The record statement for a spec inside the contract, at catalog point ``as_of``."""
    chosen = driver(spec)
    params: dict[str, Any] = {"tenant": tenant, "kinds": list(spec.kinds), "as_of": as_of}
    where = [
        "r.tenant_id = %(tenant)s",
        "r.kind = ANY(%(kinds)s)",
        "r.registration_key <= %(as_of)s",
    ]
    if spec.packages is not None:
        params["packages"] = list(spec.packages)
        where.append("r.package_id = ANY(%(packages)s)")
    series = spec.series is not None
    subject = "series" if series else "record"
    if spec.window is not None:
        window = spec.window
        params |= {"clock": window.clock, "first": window.first, "last": window.last}
        where.append(_SERIES_WINDOW if series else _WINDOW.format(t="r"))
    box = ""
    if spec.frame is not None:
        frame = spec.frame
        kind, text = reference_text(frame.reference)
        unit = frame.unit
        params |= {
            "reference_kind": kind,
            "reference": text,
            "unit": unit,
            "scope": f"{kind} {text} {unit}",
            "x0": float(frame.low[0]),
            "y0": float(frame.low[1]),
            "x1": float(frame.high[0]),
            "y1": float(frame.high[1]),
        }
        box = _BOX
        if len(frame.low) == 3:
            params |= {"z0": float(frame.low[2]), "z1": float(frame.high[2])}
            box += _BOX_3D
        if chosen != "frame":
            where.append(
                "EXISTS (SELECT 1 FROM spatial_extent s WHERE s.tenant_id = r.tenant_id"
                " AND s.record_id = r.record_id AND s.package_id = r.package_id"
                f" AND s.kind = r.kind AND {box})"
            )
    where.append(
        "(r.kind, r.record_id, r.package_id) > (%(after_kind)s::text, %(after_record)s::text,"
        " %(after_package)s::text)"
    )
    tail = (
        f" WHERE {' AND '.join(where)} ORDER BY r.kind, r.record_id, r.package_id LIMIT %(limit)s"
    )
    if chosen == "kinds":
        return RecordPlan(chosen, f"SELECT {ROW} FROM record r{tail}", params)
    if chosen == "thread":
        params["thread_id"] = spec.thread_id
        driving = (
            "SELECT m.kind, m.record_id, m.package_id FROM thread_member m"
            " WHERE m.tenant_id = %(tenant)s AND m.thread_id = %(thread_id)s"
            " AND m.registration_key <= %(as_of)s AND m.kind = ANY(%(kinds)s)"
        )
        if spec.window is not None and not series:
            driving += " AND " + _WINDOW.format(t="m")
    elif chosen == "window":
        driving = (
            "SELECT t.kind, t.record_id, t.package_id FROM time_interval t"
            f" WHERE t.tenant_id = %(tenant)s AND t.subject = '{subject}'"
            " AND t.span && box(point(index_key(%(clock)s), %(first)s::float8),"
            " point(index_key(%(clock)s), %(last)s::float8))"
            " AND t.clock = %(clock)s AND t.kind = ANY(%(kinds)s)"
            " AND t.registration_key <= %(as_of)s"
        )
    elif chosen == "frame":
        driving = (
            "SELECT DISTINCT s.kind, s.record_id, s.package_id FROM spatial_extent s"
            " WHERE s.tenant_id = %(tenant)s"
            " AND s.scope && box(point(index_key(%(scope)s), 0), point(index_key(%(scope)s), 0))"
            " AND s.xy && box(point(%(x0)s, %(y0)s), point(%(x1)s, %(y1)s))"
            f" AND s.kind = ANY(%(kinds)s) AND {box}"
        )
    else:
        driving = (
            "SELECT p.kind, p.record_id, p.package_id FROM record p"
            " WHERE p.tenant_id = %(tenant)s AND p.package_id = ANY(%(packages)s)"
            " AND p.kind = ANY(%(kinds)s) AND p.registration_key <= %(as_of)s"
        )
    statement = (
        f"WITH driving AS MATERIALIZED ({driving}) SELECT {ROW} FROM driving d"
        " JOIN record r ON r.tenant_id = %(tenant)s AND r.kind = d.kind"
        f" AND r.record_id = d.record_id AND r.package_id = d.package_id{tail}"
    )
    return RecordPlan(chosen, statement, params)
