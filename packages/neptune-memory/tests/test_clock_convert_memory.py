"""``schema.clocks.convert``: exact, through mapping claims, never estimated (ADR 0011 §3)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from memory_time_records import (
    BOTH_PLAN,
    MICRO,
    MILLI,
    build,
    clock,
    domain,
    drone_flight,
    estimate,
    mapping,
    reader,
    revised,
    two_sites,
)
from neptune.model.knowledge import Ambiguous, Known, Unknown
from neptune.model.time import INT64_MAX, INT64_MIN, Epoch, Timescale
from neptune_memory.schema.clocks import Conversion, Converted, convert
from neptune_memory.schema.interval import ledger_tx
from neptune_memory.schema.reader import AsOfBeyondHeadError

BOOT, GPS = clock("px4 boot"), clock("px4 gps")


def known(conversion: Conversion) -> Converted:
    assert isinstance(conversion.result, Known), conversion
    assert conversion.missing is None
    return conversion.result.value


def test_a_drone_boot_instant_converts_to_gps_time_exactly_with_its_stated_bound() -> None:
    graph = reader(build({"flight-17": drone_flight()}))
    out = known(convert(graph, 520_000_000, BOOT, GPS, ledger_tx(1)))
    assert out.ticks == Fraction(1_400_000_508_000) and out.clock == GPS
    assert out.bound == Known(Fraction(2))  # the sync's residual, in GPS ms
    assert out.backward == (False,) and not out.inferred
    assert [c.predicate for c in out.path] == ["clock_map"]


def test_the_inverse_is_exact_and_its_bound_is_in_source_ticks() -> None:
    graph = reader(build({"flight-17": drone_flight()}))
    out = known(convert(graph, 1_400_000_508_000, GPS, BOOT, ledger_tx(1)))
    assert out.ticks == Fraction(520_000_000) and out.backward == (True,)
    assert out.bound == Known(Fraction(2_000))  # 2 ms is 2000 boot µs
    between = known(convert(graph, 1_400_000_508_001, GPS, BOOT, ledger_tx(1)))
    assert between.ticks == Fraction(520_001_000)
    odd = known(convert(graph, 520_000_001, BOOT, GPS, ledger_tx(1)))
    assert odd.ticks == Fraction(1_400_000_508_000) + Fraction(1, 1000)  # never rounded


def test_an_instant_outside_the_mapping_is_unknown_and_names_the_missing_hop() -> None:
    graph = reader(build({"flight-17": drone_flight()}))
    result = convert(graph, 900_000_001, BOOT, GPS, ledger_tx(1))  # validity ends here
    assert isinstance(result.result, Unknown) and result.missing is not None
    assert result.missing.reached == (BOOT,) and result.missing.target == GPS
    (blocked,) = result.missing.outside_validity
    assert blocked.predicate == "clock_map" and result.missing.parameters_unknown == ()
    assert isinstance(convert(graph, 11_999_999, BOOT, GPS, ledger_tx(1)).result, Unknown)


def test_a_mapping_without_a_stated_rate_converts_nothing_and_says_why() -> None:
    graph = reader(build({"p": [mapping("half", "px4 boot", "px4 gps", anchor=(0, 0), rate=None)]}))
    result = convert(graph, 5, BOOT, GPS, ledger_tx(1))
    assert result.missing is not None and len(result.missing.parameters_unknown) == 1


def test_a_revised_mapping_applies_before_and_after_the_revision_in_its_build() -> None:
    graph = reader(revised(2))
    spot, dock = clock("spot boot"), clock("dock")
    assert known(convert(graph, 500, spot, dock, ledger_tx(2))).ticks == 50_500
    assert known(convert(graph, 1_500, spot, dock, ledger_tx(2))).ticks == 50_530


def test_history_before_the_revision_converts_through_the_old_mapping() -> None:
    graph = reader(revised(1), revised(2))
    spot, dock = clock("spot boot"), clock("dock")
    assert known(convert(graph, 1_500, spot, dock, ledger_tx(1))).ticks == 51_500


@pytest.mark.xfail(
    strict=True,
    reason="GAP MVL-132: the version emitted open at tx 1 stays current until withdrawal",
)
def test_after_the_revision_the_history_converts_through_the_new_mapping_only() -> None:
    graph = reader(revised(1), revised(2))
    spot, dock = clock("spot boot"), clock("dock")
    assert known(convert(graph, 1_500, spot, dock, ledger_tx(2))).ticks == 50_530


def test_civil_time_at_one_site_converts_to_a_machine_clock_at_another_through_gps() -> None:
    graph = reader(build(two_sites()))
    site_b, boot = clock("warehouse-b ntp"), clock("amr-12 boot")
    out = known(convert(graph, 1_790_000_000_500_000, site_b, boot, ledger_tx(1)))
    assert out.ticks == Fraction(500_003_000)  # site B is 3 µs ahead of site A through GPS
    assert out.backward == (False, True, True)  # B -> GPS, GPS <- A, A <- boot
    assert out.bound == Known(Fraction(40_000))  # the chrony log's 40 µs, in boot ns
    back = known(convert(graph, 500_003_000, boot, site_b, ledger_tx(1)))
    assert back.ticks == Fraction(1_790_000_000_500_000)


def test_a_machine_no_mapping_reaches_is_unknown_with_the_clocks_that_were_reached() -> None:
    graph = reader(build(two_sites()))
    site_b, other = clock("warehouse-b ntp"), clock("amr-31 boot")
    result = convert(graph, 1_790_000_000_500_000, site_b, other, ledger_tx(1))
    assert isinstance(result.result, Unknown) and result.missing is not None
    assert set(result.missing.reached) == {
        site_b,
        clock("gps time"),
        clock("warehouse-a ntp"),
        clock("amr-12 boot"),
    }
    assert result.missing.target == other
    unmapped = clock("nowhere")
    lonely = convert(graph, 0, unmapped, other, ledger_tx(1))
    assert lonely.missing is not None and lonely.missing.reached == (unmapped,)


def test_declared_mappings_are_preferred_and_estimates_say_they_are_inferred() -> None:
    packages = {
        "flight-17": [
            domain("px4 boot", MICRO),
            domain("px4 gps", MILLI, timescale=Timescale.GPS, epoch=Epoch.GPS),
            estimate("fit", "px4 boot", "px4 gps", anchor=(0, 100), rate=Fraction(1, 1000)),
        ]
    }
    estimated = reader(build(packages, plan=BOTH_PLAN))
    guess = known(convert(estimated, 2_000, BOOT, GPS, ledger_tx(1)))
    assert guess.ticks == 102 and guess.inferred and guess.bound == Unknown()
    assert isinstance(
        convert(estimated, 2_000, BOOT, GPS, ledger_tx(1), include_inferred=False).result, Unknown
    )
    packages["flight-17"].append(
        mapping("stated", "px4 boot", "px4 gps", anchor=(0, 99), rate=Fraction(1, 1000))
    )
    both = reader(build(packages, plan=BOTH_PLAN))
    stated = known(convert(both, 2_000, BOOT, GPS, ledger_tx(1)))
    assert stated.ticks == 101 and not stated.inferred


def test_declarations_that_disagree_at_an_instant_are_ambiguous_never_picked() -> None:
    records = [
        mapping("log says", "px4 boot", "px4 gps", anchor=(0, 100)),
        mapping("manifest says", "px4 boot", "px4 gps", anchor=(0, 105)),
    ]
    result = convert(reader(build({"p": records})), 7, BOOT, GPS, ledger_tx(1)).result
    assert isinstance(result, Ambiguous)
    assert sorted(c.value.ticks for c in result.candidates) == [107, 112]


def test_one_clock_is_itself_and_no_hop_is_no_conversion() -> None:
    graph = reader(build({"flight-17": drone_flight()}))
    same = known(convert(graph, 42, BOOT, BOOT, ledger_tx(1)))
    assert (same.ticks, same.path, same.bound) == (Fraction(42), (), Known(Fraction(0)))
    none = convert(graph, 520_000_000, BOOT, GPS, ledger_tx(1), max_hops=0)
    assert isinstance(none.result, Unknown)


@pytest.mark.parametrize("ticks", [INT64_MAX + 1, INT64_MIN - 1, True, 1.5])
def test_ticks_outside_signed_64_bit_are_a_caller_error(ticks: object) -> None:
    graph = reader(build({"flight-17": drone_flight()}))
    with pytest.raises(ValueError, match="64-bit"):
        convert(graph, ticks, BOOT, GPS, ledger_tx(1))  # type: ignore[arg-type]


def test_other_caller_errors_raise() -> None:
    graph = reader(build({"flight-17": drone_flight()}))
    with pytest.raises(ValueError, match="max_hops"):
        convert(graph, 0, BOOT, GPS, ledger_tx(1), max_hops=-1)
    with pytest.raises(ValueError):
        convert(graph, 0, "boot", GPS, ledger_tx(1))  # type: ignore[arg-type]
    with pytest.raises(AsOfBeyondHeadError):
        convert(graph, 0, BOOT, GPS, ledger_tx(2))


def test_conversion_is_deterministic() -> None:
    first = convert(
        reader(build(two_sites())), 7, clock("warehouse-b ntp"), clock("amr-12 boot"), ledger_tx(1)
    )
    again = convert(
        reader(build(two_sites())), 7, clock("warehouse-b ntp"), clock("amr-12 boot"), ledger_tx(1)
    )
    assert first == again
