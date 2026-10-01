"""A manifest's source rules set against the probe engine's observations (ADR 0047 §5, §6)."""

import pytest

from neptune.adapters.builtin import default_registry
from neptune.adapters.contract import ConfigError, ProbeResult
from neptune.adapters.registry import Candidate, select
from neptune.derived.grouping import GroupingConfig
from neptune.identity.hashing import content_id
from neptune.manifest import LoadedManifest, ManifestError, parse_bytes
from neptune.model.finding import Severity
from neptune.model.jsonvalue import JsonValue
from neptune.model.source import LocalPath
from neptune.runtime.declared import (
    ADAPTER_PINNED,
    PIN_OVERRIDES_PROBE,
    PIN_REFUSED,
    RULE_UNMATCHED,
    RULES_CONFLICT,
    Declarations,
)

SOURCE = content_id(b"step,**joint**,[spec](spec.pdf)\n")


def loaded(text: str) -> LoadedManifest:
    data = text.encode()
    return LoadedManifest(
        parse_bytes(data, "neptune.yaml"), LocalPath("neptune.yaml"), content_id(data), len(data)
    )


def build(
    text: str,
    config: dict[str, dict[str, JsonValue]] | None = None,
    grouping: GroupingConfig | None = None,
) -> Declarations:
    return Declarations.build(
        loaded(text), default_registry(), config or {}, grouping or GroupingConfig()
    )


def candidate(adapter: str, confidence: float) -> Candidate:
    return Candidate(adapter, "0.1.0", ProbeResult(confidence, ()))


TIE = select([candidate("markdown", 0.7), candidate("tabular", 0.7), candidate("text", 0.4)])
WON = select([candidate("tabular", 0.7), candidate("text", 0.4)])
NOTES = (LocalPath("arm/joint_notes.txt"),)


def choose(text: str, selection=TIE, locations=NOTES):  # type: ignore[no-untyped-def]
    declared = build(text)
    return declared, declared.choose(SOURCE, 32, locations, selection)


def test_a_pin_resolves_a_tie_with_an_info_finding() -> None:
    _, choice = choose("neptune: 1\nsources:\n  - {path: arm/joint_notes.txt, adapter: markdown}\n")
    assert choice.candidate is not None and choice.candidate.adapter == "markdown"
    assert choice.answers_tie
    (finding,) = choice.findings
    assert finding.code == ADAPTER_PINNED and finding.severity is Severity.INFO
    assert finding.details["manifest_pointer"] == "/sources/0"
    assert finding.related[0].locator[0].to_json() == {
        "kind": "json_pointer",
        "pointer": "/sources/0",
    }


def test_a_pin_that_agrees_with_the_probe_says_nothing() -> None:
    _, choice = choose("neptune: 1\nsources:\n  - {glob: '**/*.txt', adapter: tabular}\n", WON)
    assert choice.candidate is not None and choice.findings == () and not choice.answers_tie


def test_overriding_the_probes_ranking_is_a_warning() -> None:
    _, choice = choose("neptune: 1\nsources:\n  - {path: arm, adapter: text}\n", WON)
    assert choice.candidate is not None and choice.candidate.adapter == "text"
    (finding,) = choice.findings
    assert finding.code == PIN_OVERRIDES_PROBE and finding.severity is Severity.WARNING


def test_a_pin_the_probe_declines_is_refused() -> None:
    _, choice = choose("neptune: 1\nsources:\n  - {path: arm, adapter: image}\n")
    assert choice.candidate is None and choice.config is None
    (finding,) = choice.findings
    assert finding.code == PIN_REFUSED and finding.details["probe_status"] == "ambiguous"


def test_the_last_matching_rule_applies() -> None:
    text = (
        "neptune: 1\nsources:\n  - {glob: '**', adapter: text}\n"
        "  - {path: arm, adapter: markdown}\n"
    )
    _, choice = choose(text)
    assert choice.rule is not None and choice.rule.pointer == "/sources/1"


def test_locations_of_one_artifact_must_agree() -> None:
    text = (
        "neptune: 1\nsources:\n  - {path: a, adapter: markdown}\n  - {path: b, adapter: tabular}\n"
    )
    _, choice = choose(text, locations=(LocalPath("a/n.txt"), LocalPath("b/n.txt")))
    assert choice.candidate is None
    assert [f.code for f in choice.findings] == [RULES_CONFLICT]


def test_no_rule_no_choice() -> None:
    _, choice = choose("neptune: 1\nsources:\n  - {path: elsewhere, adapter: text}\n")
    assert choice.candidate is None and choice.findings == ()


def test_unmatched_rules_are_findings() -> None:
    declared = build(
        "neptune: 1\nsources:\n  - {path: arm, adapter: text}\n  - {path: gone, adapter: text}\n"
    )
    declared.choose(SOURCE, 32, NOTES, TIE)
    (finding,) = declared.unmatched(NOTES)
    assert finding.code == RULE_UNMATCHED and finding.details["pattern"] == "gone"


def test_rule_options_resolve_per_rule() -> None:
    declared = build(
        "neptune: 1\nsources:\n  - {path: a, adapter: tabular, options: {csv_delimiter: ';'}}\n"
        "  - {path: b, adapter: tabular}\n"
    )
    (_, first), (_, second) = declared.rules
    assert first.values["csv_delimiter"] == ";" and first.transform.id != second.transform.id


def test_manifest_adapter_options_join_the_jobs() -> None:
    declared = build("neptune: 1\nadapters:\n  tabular: {options: {csv_delimiter: ';'}}\n")
    assert declared.config == {"tabular": {"csv_delimiter": ";"}}
    with pytest.raises(ManifestError, match="set by the job and the manifest"):
        build(
            "neptune: 1\nadapters:\n  tabular: {options: {csv_delimiter: ';'}}\n",
            {"tabular": {"csv_delimiter": ","}},
        )


@pytest.mark.parametrize(
    ("text", "error", "says"),
    [
        (
            "neptune: 1\nsources:\n  - {path: a, adapter: ghost}\n",
            ManifestError,
            "no adapter 'ghost'",
        ),
        ("neptune: 1\nadapters:\n  ghost: {options: {}}\n", ManifestError, "no adapter 'ghost'"),
        (
            "neptune: 1\nsources:\n  - {path: a, adapter: tabular, options: {nope: 1}}\n",
            ConfigError,
            "/sources/0/options",
        ),
        (
            "neptune: 1\nsources:\n  - {path: a, adapter: tabular, options: {csv_delimiter: 7}}\n",
            ConfigError,
            "/sources/0/options",
        ),
    ],
)
def test_unusable_rules_fail_before_work(text: str, error: type[Exception], says: str) -> None:
    with pytest.raises(error, match=says):
        build(text)


def test_runs_become_declared_sessions_under_the_manifest() -> None:
    declared = build(
        "neptune: 1\nruns:\n  - {name: pick, paths: [arm]}\ngrouping: {gap_seconds: 5}\n"
    )
    assert [s.name for s in declared.grouping.sessions] == ["pick"]
    assert declared.grouping.gap_seconds == 5
    assert declared.grouper().transform.upstream == (declared.loaded.transform.id,)
    with pytest.raises(ManifestError, match="configure grouping"):
        build("neptune: 1\n", grouping=GroupingConfig(gap_seconds=5))


def test_the_manifest_transform_names_its_bytes() -> None:
    one, two = loaded("neptune: 1\n"), loaded("neptune: 1\n# edited\n")
    assert one.transform.config["source"] == one.content_id
    assert one.transform.id != two.transform.id  # an edit is a new lineage
    assert loaded("neptune: 1\n").transform.id == one.transform.id
