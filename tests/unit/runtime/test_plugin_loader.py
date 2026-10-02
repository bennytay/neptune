"""Loading plugin adapters and Sources from installed distributions' entry points (ADR 0058).

Every distribution here is really installed into a site directory (``make_plugin_dists``) and
read by ``importlib.metadata``; nothing is mocked.
"""

from pathlib import Path
from types import ModuleType

import pytest

from neptune.adapters.contract import configure
from neptune.adapters.registry import AdapterRegistry
from neptune.model.finding import FindingCategory, Severity
from neptune.model.ids import ExternalObjectRef
from neptune.runtime.plugins import (
    ADAPTERS_GROUP,
    DUPLICATE_ID,
    LOAD_FAILED,
    MAX_OUTPUT,
    NO_PLUGINS,
    OUTPUT,
    PLUGINS_ID,
    REFUSED,
    SOURCES_GROUP,
    PluginAdapter,
    PluginPolicy,
    Plugins,
    load_plugins,
    normalise,
    plugins_transform,
)

pytestmark = pytest.mark.usefixtures("forget_plugins")


def _load(site: Path, policy: PluginPolicy | None = None, **kwargs: object) -> Plugins:
    return load_plugins(policy or PluginPolicy(), path=[str(site)], **kwargs)  # type: ignore[arg-type]


def _codes(plugins: Plugins) -> list[tuple[str, str, object]]:
    return sorted(
        (f.code, str(f.details["entry_point"]), f.details.get("reason")) for f in plugins.findings
    )


def test_an_installed_adapter_is_admitted_and_names_its_distribution(
    plugin_dists: ModuleType, plugin_site: Path
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-tally",
        "1.2.0",
        adapters={"tally": ":TallyAdapter"},
        module=plugin_dists.TALLY,
    )
    plugins = _load(plugin_site)
    assert plugins.findings == () and plugins.sources == ()
    (adapter,) = plugins.adapters
    assert isinstance(adapter, PluginAdapter)
    assert adapter.descriptor.id == "tally" and adapter.descriptor.version == "1.0.0"
    assert adapter.descriptor.libraries == (("neptune-test-tally", "1.2.0"),)
    assert (adapter.origin.group, adapter.origin.distribution) == (
        ADAPTERS_GROUP,
        "neptune-test-tally",
    )
    # The distribution and its version are in the transform, so in every chunk id and cache key.
    transform = configure(adapter.descriptor).transform
    bare = configure(adapter.adapter.descriptor).transform
    assert transform.adapter_id == "tally" and transform.adapter_version == "1.0.0"
    assert dict(transform.libraries) == {"neptune-test-tally": "1.2.0"}
    assert transform.id != bare.id
    AdapterRegistry([adapter])  # the registry admits the wrapped adapter as it is


def test_a_new_plugin_version_is_a_new_transform(
    plugin_dists: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transforms = []
    for version in ("1.0.0", "1.0.1"):
        site = tmp_path / version
        plugin_dists.install(
            site,
            "neptune-test-tally",
            version,
            adapters={"tally": ":TallyAdapter"},
            module=plugin_dists.TALLY,
        )
        monkeypatch.syspath_prepend(str(site))
        (adapter,) = _load(site).adapters
        transforms.append(configure(adapter.descriptor).transform.id)
    assert transforms[0] != transforms[1]


def test_the_order_of_the_path_changes_nothing(
    plugin_dists: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    plugin_dists.install(
        first,
        "neptune-test-tally",
        "1.0.0",
        adapters={"tally": ":TallyAdapter"},
        module=plugin_dists.TALLY,
    )
    plugin_dists.install(
        second,
        "neptune-test-framelog",
        "2.0.0",
        adapters={"framelog": ":FrameLogAdapter"},
        module=plugin_dists.FRAMELOG,
    )
    plugin_dists.install(
        second,
        "neptune-test-broken",
        "0.1.0",
        adapters={"broken": ":make"},
        module=plugin_dists.BROKEN_IMPORT,
    )
    for site in (first, second):
        monkeypatch.syspath_prepend(str(site))

    def seen(path: list[Path]) -> tuple[object, ...]:
        plugins = load_plugins(path=[str(p) for p in path])
        return (
            [configure(a.descriptor).transform for a in plugins.adapters],
            plugins.findings,
            plugins.transform,
        )

    assert seen([first, second]) == seen([second, first])
    adapters, findings, _ = seen([second, first])
    assert [t.adapter_id for t in adapters] == ["framelog", "tally"]  # type: ignore[attr-defined]
    assert [f.code for f in findings] == [LOAD_FAILED]  # type: ignore[attr-defined]


def test_a_plugin_that_does_not_import_is_a_finding_and_the_rest_load(
    plugin_dists: ModuleType, plugin_site: Path
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-broken",
        "0.1.0",
        adapters={"broken": ":make"},
        module=plugin_dists.BROKEN_IMPORT,
    )
    plugin_dists.install(
        plugin_site,
        "neptune-test-tally",
        "1.0.0",
        adapters={"tally": ":TallyAdapter"},
        module=plugin_dists.TALLY,
    )
    plugins = _load(plugin_site)
    assert [a.descriptor.id for a in plugins.adapters] == ["tally"]
    (finding,) = plugins.findings
    assert (finding.code, finding.category, finding.severity) == (
        LOAD_FAILED,
        FindingCategory.FAILED,
        Severity.WARNING,
    )
    assert finding.subject == ExternalObjectRef(
        PLUGINS_ID, "neptune.adapters/neptune-test-broken/broken", "0.1.0"
    )
    assert finding.transform == plugins.transform.id
    assert plugins.transform == plugins_transform(loaded={"neptune-test-tally": "1.0.0"})
    assert finding.details == {
        "distribution": "neptune-test-broken",
        "entry_point": "broken",
        "error": "ModuleNotFoundError",
        "group": "neptune.adapters",
        "step": "imported",
        "version": "0.1.0",
    }
    assert "neptune_test_dependency" not in finding.message  # never the exception's text


def test_a_factory_that_raises_is_a_finding_without_its_message(
    plugin_dists: ModuleType, plugin_site: Path
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-raising",
        "0.1.0",
        adapters={"raising": ":make"},
        module=plugin_dists.RAISING_FACTORY,
    )
    (finding,) = _load(plugin_site).findings
    assert finding.code == LOAD_FAILED
    assert (finding.details["error"], finding.details["step"]) == ("RuntimeError", "built")
    assert "/home" not in finding.message and "/home" not in str(finding.details)


@pytest.mark.parametrize(
    ("name", "value", "module", "reason"),
    [
        ("answer", ":answer", "NOT_AN_ADAPTER", "not_callable"),
        ("hollow", ":NotAnAdapter", "NOT_AN_ADAPTER", "no_descriptor"),
        ("counter", ":TallyAdapter", "TALLY", "name_mismatch"),
        ("tally_next", ":TallyAdapter", "OTHER_ABI", "other_abi"),
        ("bad-Name", ":TallyAdapter", "TALLY", "invalid_name"),
    ],
)
def test_an_inadmissible_plugin_is_refused_with_its_reason(
    plugin_dists: ModuleType, plugin_site: Path, name: str, value: str, module: str, reason: str
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-odd",
        "0.1.0",
        adapters={name: value},
        module=getattr(plugin_dists, module),
    )
    plugins = _load(plugin_site)
    assert plugins.adapters == ()
    assert _codes(plugins) == [(REFUSED, name, reason)]


@pytest.mark.parametrize("spelling", ["neptune-test-tally", "Neptune_Test.Tally"])
def test_a_library_pin_that_contradicts_the_distribution_is_refused(
    plugin_dists: ModuleType, plugin_site: Path, spelling: str
) -> None:
    module = plugin_dists.TALLY.replace("libraries=()", f'libraries=(("{spelling}", "9.9.9"),)')
    plugin_dists.install(
        plugin_site,
        "neptune-test-tally",
        "1.0.0",
        adapters={"tally": ":TallyAdapter"},
        module=module,
    )
    plugins = _load(plugin_site)
    assert _codes(plugins) == [(REFUSED, "tally", "library_conflict")]
    assert plugins.findings[0].details["pinned"] == "9.9.9"


def test_a_library_pin_that_agrees_is_listed_once(
    plugin_dists: ModuleType, plugin_site: Path
) -> None:
    module = plugin_dists.TALLY.replace(
        "libraries=()", 'libraries=(("Neptune_Test_Tally", "1.0.0"), ("numpy", "2.1.0"))'
    )
    plugin_dists.install(
        plugin_site,
        "neptune-test-tally",
        "1.0.0",
        adapters={"tally": ":TallyAdapter"},
        module=module,
    )
    (adapter,) = _load(plugin_site).adapters
    assert adapter.descriptor.libraries == (("neptune-test-tally", "1.0.0"), ("numpy", "2.1.0"))


def test_a_plugin_never_takes_a_built_in_id(plugin_dists: ModuleType, plugin_site: Path) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-tally",
        "1.0.0",
        adapters={"tally": ":TallyAdapter"},
        module=plugin_dists.TALLY,
    )
    plugins = _load(plugin_site, reserved=["tally", "text"])
    assert plugins.adapters == ()
    (finding,) = plugins.findings
    assert (finding.code, finding.category) == (DUPLICATE_ID, FindingCategory.AMBIGUOUS)
    assert finding.details["claimants"] == ["built-in", "neptune-test-tally:tally"]


def test_two_plugins_with_one_id_are_both_refused(
    plugin_dists: ModuleType, plugin_site: Path
) -> None:
    for dist in ("neptune-test-tally", "neptune-test-tally-fork"):
        plugin_dists.install(
            plugin_site,
            dist,
            "1.0.0",
            adapters={"tally": ":TallyAdapter"},
            module=plugin_dists.TALLY,
        )
    plugins = _load(plugin_site)
    assert plugins.adapters == ()
    assert [f.code for f in plugins.findings] == [DUPLICATE_ID, DUPLICATE_ID]
    assert {f.details["distribution"] for f in plugins.findings} == {
        "neptune-test-tally",
        "neptune-test-tally-fork",
    }
    for finding in plugins.findings:
        assert finding.details["claimants"] == [
            "neptune-test-tally-fork:tally",
            "neptune-test-tally:tally",
        ]


def test_the_policy_decides_which_distributions_are_read(
    plugin_dists: ModuleType, plugin_site: Path
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-tally",
        "1.0.0",
        adapters={"tally": ":TallyAdapter"},
        module=plugin_dists.TALLY,
    )
    plugin_dists.install(
        plugin_site,
        "neptune-test-broken",
        "0.1.0",
        adapters={"broken": ":make"},
        module=plugin_dists.BROKEN_IMPORT,
    )
    assert _load(plugin_site, NO_PLUGINS) == Plugins(transform=plugins_transform(NO_PLUGINS))
    allowed = PluginPolicy(allow=("Neptune_Test.Tally",))
    assert allowed.allow == ("neptune-test-tally",)
    plugins = _load(plugin_site, allowed)
    # A distribution the policy leaves out leaves no trace, not even its broken plugin.
    assert [a.descriptor.id for a in plugins.adapters] == ["tally"] and plugins.findings == ()
    assert plugins.transform.config == {"allow": ["neptune-test-tally"]}
    assert _load(plugin_site, PluginPolicy(allow=())).adapters == ()


@pytest.mark.parametrize("allow", ["neptune-test-tally", ("bad name",), (3,)])
def test_a_policy_names_distributions(allow: object) -> None:
    with pytest.raises(ValueError):
        PluginPolicy(allow=allow)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PluginPolicy(enabled="yes")  # type: ignore[arg-type]


def test_sources_are_admitted_uncalled_and_refused_like_adapters(
    plugin_dists: ModuleType, plugin_site: Path
) -> None:
    calls = "calls = []\n\ndef connector(*args):\n    calls.append(args)\n    return object()\n"
    plugin_dists.install(
        plugin_site,
        "neptune-test-store",
        "3.0.0",
        sources={"objstore": ":connector", "notone": ":calls", "twice": ":connector"},
        module=calls,
    )
    plugin_dists.install(
        plugin_site,
        "neptune-test-other-store",
        "1.0.0",
        sources={"twice": ":connector"},
        module=calls,
    )
    plugins = _load(plugin_site, groups=[SOURCES_GROUP])
    (source,) = plugins.sources
    assert (source.id, source.origin.distribution, source.origin.version) == (
        "objstore",
        "neptune-test-store",
        "3.0.0",
    )
    assert source.origin.group == SOURCES_GROUP
    assert callable(source.factory)
    import neptune_test_store  # type: ignore[import-not-found]

    assert neptune_test_store.calls == []  # admitted, never called
    assert _codes(plugins) == [
        (DUPLICATE_ID, "twice", None),
        (DUPLICATE_ID, "twice", None),
        (REFUSED, "notone", "not_callable"),
    ]


def test_only_the_named_groups_are_read(plugin_dists: ModuleType, plugin_site: Path) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-broken",
        "0.1.0",
        adapters={"broken": ":make"},
        module=plugin_dists.BROKEN_IMPORT,
    )
    assert _load(plugin_site, groups=[SOURCES_GROUP]).findings == ()
    with pytest.raises(ValueError):
        _load(plugin_site, groups=["neptune.other"])
    with pytest.raises(TypeError):
        load_plugins(True)  # type: ignore[arg-type]


def test_one_distribution_twice_on_the_path_is_the_first(
    plugin_dists: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sites = [tmp_path / "new", tmp_path / "old"]
    for site, version in zip(sites, ("2.0.0", "1.0.0"), strict=True):
        plugin_dists.install(
            site,
            "neptune-test-tally",
            version,
            adapters={"tally": ":TallyAdapter"},
            module=plugin_dists.TALLY,
        )
    monkeypatch.syspath_prepend(str(sites[0]))
    (adapter,) = load_plugins(path=[str(site) for site in sites]).adapters
    assert adapter.origin.version == "2.0.0"  # the one ``import`` loads


def test_names_are_normalised_as_packaging_compares_them() -> None:
    assert normalise("Neptune_Deploy") == normalise("neptune.deploy") == "neptune-deploy"


def test_loading_twice_gives_the_same_findings(plugin_dists: ModuleType, plugin_site: Path) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-raising",
        "0.1.0",
        adapters={"raising": ":make"},
        module=plugin_dists.RAISING_FACTORY,
    )
    assert _load(plugin_site).findings == _load(plugin_site).findings


def test_the_loader_transform_names_every_distribution_it_admitted_from(
    plugin_dists: ModuleType, plugin_site: Path
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-tally",
        "1.0.0",
        adapters={"tally": ":TallyAdapter"},
        module=plugin_dists.TALLY,
    )
    plugin_dists.install(
        plugin_site,
        "Neptune_Test_Store",
        "3.0.0",
        sources={"objstore": ":connector"},
        module="def connector():\n    return None\n",
    )
    plugin_dists.install(
        plugin_site,
        "neptune-test-broken",
        "0.1.0",
        adapters={"broken": ":make"},
        module=plugin_dists.BROKEN_IMPORT,
    )
    plugins = _load(plugin_site)
    # Sorted, normalised, and only what was admitted: the broken one is a finding instead.
    assert plugins.loaded == (("neptune-test-store", "3.0.0"), ("neptune-test-tally", "1.0.0"))
    assert plugins.transform.libraries == plugins.loaded
    assert plugins.distribution_of("tally") == "neptune-test-tally 1.0.0"
    assert plugins.distribution_of("text") is None
    assert _load(plugin_site, NO_PLUGINS).loaded == ()


def test_what_a_plugin_prints_is_captured_bounded_and_kept_in_a_finding(
    plugin_dists: ModuleType, plugin_site: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-chatty",
        "0.1.0",
        adapters={"chatty": ":TallyAdapter"},
        module=plugin_dists.PRINTING,
    )
    plugins = _load(plugin_site)
    assert capfd.readouterr() == ("", "")  # nothing reached this process's stdout or stderr
    assert [a.descriptor.id for a in plugins.adapters] == ["chatty"]
    (finding,) = plugins.findings
    assert (finding.code, finding.severity) == (OUTPUT, Severity.INFO)
    output = finding.details["output"]
    assert isinstance(output, str) and len(output) == MAX_OUTPUT
    assert output.startswith("loading the chatty plugin xxx")
    assert finding.details["output_chars"] == len("loading the chatty plugin ") + 5000 + 1 + len(
        "warning: chatty\n"
    )
    assert finding.details["step"] == "imported"


@pytest.mark.parametrize(("module", "error"), [("BOOM", "Boom"), ("EXITING", "SystemExit")])
def test_a_base_exception_at_import_is_a_finding(
    plugin_dists: ModuleType, plugin_site: Path, module: str, error: str
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-boom",
        "0.1.0",
        adapters={"boom": ":make"},
        module=getattr(plugin_dists, module),
    )
    (finding,) = _load(plugin_site).findings
    assert (finding.code, finding.details["error"]) == (LOAD_FAILED, error)


def test_a_malformed_entry_points_file_is_a_finding_naming_its_distribution(
    plugin_dists: ModuleType, plugin_site: Path
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-garbled",
        "0.2.0",
        entry_points="[neptune.adapters]\nthis is not ini\n",
    )
    # A distribution that is no plugin may have a broken file too: none of Neptune's business.
    plugin_dists.install(
        plugin_site, "unrelated-tool", "1.0.0", entry_points="[console_scripts]\nnot ini\n"
    )
    plugins = _load(plugin_site)
    (finding,) = plugins.findings
    assert finding.code == LOAD_FAILED
    assert finding.subject == ExternalObjectRef(PLUGINS_ID, "neptune-test-garbled", "0.2.0")
    assert finding.details["step"] == "listed"
    assert finding.details["distribution"] == "neptune-test-garbled"
    assert "entry_point" not in finding.details
    assert plugins.unmatched == ()


def test_distributions_with_no_usable_name_are_each_refused(
    plugin_dists: ModuleType, plugin_site: Path
) -> None:
    for package, metadata in (
        ("neptune_test_anon_a", "Metadata-Version: 2.1\nVersion: 1.0.0\n"),
        ("neptune_test_anon_b", "Metadata-Version: 2.1\nName: not a name!\nVersion: 2.0.0\n"),
    ):
        plugin_dists.install(
            plugin_site,
            package,
            "1.0.0",
            adapters={"tally": ":TallyAdapter"},
            module=plugin_dists.TALLY,
            metadata=metadata,
        )
    plugins = _load(plugin_site)
    assert plugins.adapters == () and plugins.loaded == ()
    assert [(f.code, f.details["reason"]) for f in plugins.findings] == [
        (REFUSED, "unnamed_distribution"),
        (REFUSED, "unnamed_distribution"),
    ]
    assert {f.details["value"] for f in plugins.findings} == {
        "neptune_test_anon_a:TallyAdapter",
        "neptune_test_anon_b:TallyAdapter",
    }


def test_an_allowlist_name_that_registers_nothing_is_unmatched(
    plugin_dists: ModuleType, plugin_site: Path
) -> None:
    plugin_dists.install(
        plugin_site,
        "neptune-test-tally",
        "1.0.0",
        adapters={"tally": ":TallyAdapter"},
        module=plugin_dists.TALLY,
    )
    plugin_dists.install(plugin_site, "neptune-test-empty", "1.0.0")  # installed, no plugin
    plugins = _load(
        plugin_site,
        PluginPolicy(allow=("neptune-test-tally", "neptune-test-taly", "neptune-test-empty")),
    )
    assert plugins.unmatched == ("neptune-test-empty", "neptune-test-taly")
    assert [a.descriptor.id for a in plugins.adapters] == ["tally"]
