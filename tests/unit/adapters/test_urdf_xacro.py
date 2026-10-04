"""The Xacro expander: what one file decides, what it cannot, and the bounds (ADR 0039 §4).

The oracle for a whole expansion is real xacro's output, committed as
``tests/fixtures/urdf/oracle/quadrotor.expanded.urdf``: the two trees must be equal element for
element, attribute for attribute, text for text.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

from neptune.adapters.urdf.xacro import (
    NOT_COVERED,
    UNKNOWN,
    Expansion,
    ExpansionLimit,
    expand,
    is_xacro,
)
from neptune.adapters.urdf.xmltree import Element, XmlError, parse, serialize

FIXTURES: Final = Path(__file__).parents[2] / "fixtures" / "urdf"
HEADER: Final = '<robot xmlns:xacro="http://www.ros.org/wiki/xacro" name="t">'


def tree(data: bytes) -> Element:
    return parse(data, max_depth=64, max_elements=50_000)


def run(body: str, **limits: int) -> Expansion:
    bounds: dict[str, Any] = {
        "max_depth": 64,
        "max_elements": 50_000,
        "max_chars": 1 << 24,
        **limits,
    }
    return expand(tree((HEADER + body + "</robot>").encode()), **bounds)


def shape(element: Element) -> Any:
    """An element as comparable data: name, attributes, element children, text."""
    children = [shape(child) for child in element.elements()]
    return (element.tag, sorted(element.attributes), children, element.text().strip())


def first(expansion: Expansion, tag: str) -> Element:
    return next(child for child in expansion.root.elements() if child.tag == tag)


def problems(expansion: Expansion) -> list[str]:
    return sorted(problem.name for problem in expansion.problems)


# --- Against real xacro ------------------------------------------------------------------------


def test_the_quadrotor_expands_exactly_as_real_xacro_does() -> None:
    expansion = expand(
        tree((FIXTURES / "xacro" / "quadrotor.urdf.xacro").read_bytes()),
        max_depth=64,
        max_elements=50_000,
        max_chars=1 << 24,
    )
    oracle = tree((FIXTURES / "oracle" / "quadrotor.expanded.urdf").read_bytes())
    assert not expansion.problems
    assert shape(expansion.root) == shape(oracle)


def test_the_expansion_serialises_to_the_same_bytes_and_ranges_every_time() -> None:
    data = (FIXTURES / "xacro" / "diff_drive.urdf.xacro").read_bytes()
    bounds: dict[str, Any] = {"max_depth": 64, "max_elements": 50_000, "max_chars": 1 << 24}
    first_run, second_run = expand(tree(data), **bounds), expand(tree(data), **bounds)
    assert serialize(first_run.root) == serialize(second_run.root)
    written = serialize(first_run.root)
    reread = tree(written)
    assert shape(reread) == shape(first_run.root)
    for element in first_run.root.elements():
        assert written[element.start : element.end].startswith(f"<{element.tag}".encode())


# --- Properties, arguments and expressions -----------------------------------------------------


def test_properties_are_lazy_typed_and_scoped() -> None:
    expansion = run(
        '<xacro:property name="a" value="${b * 2}"/>'
        '<xacro:property name="b" value="3"/>'
        '<xacro:property name="b" default="99"/>'
        '<xacro:property name="s" value="\'01\'"/>'
        '<link name="l" a="${a}" s="${s}" t="${a / 4}" u="${b == 3}"/>'
    )
    link = first(expansion, "link")
    assert dict(link.attributes) == {"name": "l", "a": "6", "s": "1", "t": "1.5", "u": "True"}
    assert not expansion.problems


def test_arguments_take_their_declared_defaults_and_are_recorded() -> None:
    expansion = run(
        '<xacro:arg name="prefix" default="left_"/>'
        '<xacro:arg name="prefix" default="ignored_"/>'
        '<xacro:arg name="scale" default="2"/>'
        '<link name="$(arg prefix)link" s="${$(arg scale) * 2}" e="$(eval 1 + 2 * scale)"/>'
    )
    assert dict(first(expansion, "link").attributes) == {"name": "left_link", "s": "4", "e": "5"}
    assert [(a.name, a.value) for a in expansion.arguments] == [("prefix", "left_"), ("scale", "2")]


def test_escapes_keep_their_text() -> None:
    link = first(run('<link name="a" b="$${x}" c="$$(arg y)" d="cost: $5"/>'), "link")
    assert dict(link.attributes) == {"name": "a", "b": "${x}", "c": "$(arg y)", "d": "cost: $5"}


def test_text_is_expanded_too() -> None:
    expansion = run('<xacro:property name="r" value="7"/><mimic>${r * 2}</mimic>')
    assert first(expansion, "mimic").text() == "14"


# --- Macros, blocks and conditionals -----------------------------------------------------------


def test_macros_take_parameters_defaults_forwarding_and_blocks() -> None:
    expansion = run(
        '<xacro:property name="side" value="left"/>'
        '<xacro:macro name="wheel" params="radius:=0.1 side:=^ tag:=^|none *origin **extra">'
        '<link name="${side}_${tag}" r="${radius}"><xacro:insert_block name="origin"/>'
        '<xacro:insert_block name="extra"/></link>'
        "</xacro:macro>"
        '<xacro:wheel radius="0.2"><origin xyz="1 2 3"/><extra><a/><b/></extra></xacro:wheel>'
    )
    link = first(expansion, "link")
    assert dict(link.attributes) == {"name": "left_none", "r": "0.2"}
    assert [child.tag for child in link.elements()] == ["origin", "a", "b"]
    assert not expansion.problems


def test_a_block_can_be_inserted_twice_and_a_block_property_is_expanded_where_inserted() -> None:
    expansion = run(
        '<xacro:property name="k" value="1"/>'
        '<xacro:property name="blk"><origin xyz="${k} 0 0"/></xacro:property>'
        '<xacro:macro name="m" params="k"><joint name="j${k}"><xacro:insert_block name="blk"/>'
        "</joint></xacro:macro>"
        '<xacro:m k="5"/><xacro:m k="6"/>'
    )
    joints = [child for child in expansion.root.elements() if child.tag == "joint"]
    origins = [joint.elements()[0].attribute("xyz") for joint in joints]
    assert origins == ["5 0 0", "6 0 0"]  # dynamic scope, as xacro does


def test_conditionals_keep_or_drop_their_content() -> None:
    expansion = run(
        '<xacro:if value="${1 + 1 == 2}"><link name="kept"/></xacro:if>'
        '<xacro:unless value="true"><link name="dropped"/></xacro:unless>'
        '<xacro:if value="0"><link name="zero"/></xacro:if>'
    )
    assert [link.attribute("name") for link in expansion.root.elements()] == ["kept"]


def test_a_condition_the_file_cannot_decide_drops_its_content_with_a_finding() -> None:
    expansion = run('<xacro:if value="$(optenv USE_SIM true)"><link name="sim"/></xacro:if>')
    assert not expansion.root.elements()
    assert problems(expansion) == ["xacro_condition_undecided", "xacro_not_covered"]


def test_a_condition_that_is_not_a_boolean_is_invalid() -> None:
    expansion = run('<xacro:if value="maybe"><link name="x"/></xacro:if>')
    assert not expansion.root.elements() and problems(expansion) == ["xacro_invalid"]


# --- What the file alone cannot decide ---------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "state", "problem"),
    [
        ("$(find pkg)/meshes/a.stl", NOT_COVERED, "xacro_not_covered"),
        ("$(env HOME)", NOT_COVERED, "xacro_not_covered"),
        ("$(optenv HOME /root)", NOT_COVERED, "xacro_not_covered"),
        ("$(dirname)/a.stl", NOT_COVERED, "xacro_not_covered"),
        ("$(cwd)", NOT_COVERED, "xacro_not_covered"),
        ("$(arg undeclared)", NOT_COVERED, "xacro_not_covered"),
        ("${undefined_property}", UNKNOWN, "xacro_undefined"),
        ("${1 +}", UNKNOWN, "xacro_invalid"),
        ("${[1, 2][0]}", UNKNOWN, "xacro_unsupported"),
        ("$(anon x)", UNKNOWN, "xacro_unsupported"),
        ("${unclosed", UNKNOWN, "xacro_invalid"),
    ],
)
def test_an_unresolved_value_keeps_its_text_and_says_why(
    value: str, state: str, problem: str
) -> None:
    expansion = run(f'<link name="l" v="{value}"/>')
    link = first(expansion, "link")
    assert link.attribute("v") == value
    assert link.unresolved == {"v": state}
    assert problems(expansion) == [problem]
    assert expansion.problems[0].element.tag == "link"  # cites the source element


def test_includes_are_never_followed() -> None:
    expansion = run(
        '<xacro:include filename="$(find pkg)/other.xacro"/><xacro:include filename="/etc/passwd"/>'
        '<xacro:other_macro/><link name="kept"/>'
    )
    assert [link.attribute("name") for link in expansion.root.elements()] == ["kept"]
    assert problems(expansion) == [
        "xacro_include_not_followed",
        "xacro_include_not_followed",
        "xacro_undefined",
    ]


@pytest.mark.parametrize(
    "call",
    [
        '<xacro:m wrong="1"/>',  # not a parameter
        "<xacro:m/>",  # a parameter without default left out
        '<xacro:m p="1"><a/><b/></xacro:m>',  # a block it has no parameter for
    ],
)
def test_an_invalid_call_is_dropped_and_reported(call: str) -> None:
    expansion = run('<xacro:macro name="m" params="p"><link name="${p}"/></xacro:macro>' + call)
    assert not expansion.root.elements()
    assert problems(expansion) == ["xacro_invalid"]


def test_unsupported_directives_are_dropped_and_reported() -> None:
    expansion = run('<xacro:element xacro:name="link"/><xacro:attribute name="a" value="b"/>')
    assert problems(expansion) == ["xacro_unsupported", "xacro_unsupported"]


def test_a_document_is_xacro_by_its_prefix() -> None:
    assert is_xacro(tree(b'<robot xmlns:xacro="http://www.ros.org/wiki/xacro"/>'))
    assert is_xacro(tree(b'<robot><xacro:property name="a" value="1"/></robot>'))
    assert not is_xacro(tree(b'<robot name="${not_xacro}"><link name="a"/></robot>'))


# --- Bounds ------------------------------------------------------------------------------------


def test_recursion_and_explosions_stop_at_the_bounds() -> None:
    with pytest.raises(ExpansionLimit, match="macro calls deeper"):
        run('<xacro:macro name="f"><xacro:f/></xacro:macro><xacro:f/>')
    with pytest.raises(ExpansionLimit, match="more elements"):
        run(
            '<xacro:macro name="a"><l/><l/><l/></xacro:macro>'
            '<xacro:macro name="b"><xacro:a/><xacro:a/><xacro:a/></xacro:macro>'
            "<xacro:b/>",
            max_elements=8,
        )
    with pytest.raises(ExpansionLimit, match="deeper than max_depth"):
        run('<xacro:macro name="n"><a><xacro:n/></a></xacro:macro><xacro:n/>', max_depth=10)
    with pytest.raises(ExpansionLimit, match="larger than max_bytes"):
        run('<link name="l" a="' + "x" * 200 + '"/>', max_chars=100)
    # A ** block's own text counts at every insertion, not only where the call gives it (and its
    # tags count too: 11 <c>s of 80 characters, <g> and <robot> stay under 1000).
    inserts = '<xacro:insert_block name="b"/>' * 10
    body = f'<xacro:macro name="m" params="**b"><g>{inserts}</g></xacro:macro><xacro:m><c>'
    assert run(body + "x" * 80 + "</c></xacro:m>", max_chars=1000)
    with pytest.raises(ExpansionLimit, match="larger than max_bytes"):
        run(body + "x" * 200 + "</c></xacro:m>", max_chars=1000)


def test_work_without_output_is_bounded_too() -> None:
    body = '<xacro:macro name="m0"><xacro:property name="p" value="1"/></xacro:macro>' + "".join(
        f'<xacro:macro name="m{n}">' + f"<xacro:m{n - 1}/>" * 10 + "</xacro:macro>"
        for n in range(1, 8)
    )
    with pytest.raises(ExpansionLimit, match="steps"):
        run(body + "<xacro:m7/>")


def test_a_hostile_document_is_refused_before_expansion() -> None:
    with pytest.raises(XmlError) as refused:
        tree((FIXTURES / "hostile" / "billion_laughs.urdf").read_bytes())
    assert refused.value.code == "doctype_refused"


def test_a_long_property_chain_is_a_finding_not_a_recursion_error() -> None:
    chain = '<xacro:property name="p0" value="1"/>' + "".join(
        f'<xacro:property name="p{n}" value="${{p{n - 1} + 1}}"/>' for n in range(1, 200)
    )
    expansion = run(chain + '<link name="l" v="${p199}" w="${p5}"/>')
    link = first(expansion, "link")
    assert link.unresolved == {"v": UNKNOWN}
    assert link.attribute("w") == "6"
    assert problems(expansion) == ["xacro_invalid"]


def at_depth(frames: int, call: Callable[[], Any]) -> Any:
    """``call()`` made ``frames`` Python frames deeper than here."""
    return call() if frames == 0 else at_depth(frames - 1, call)


def outcome(body: str) -> Any:
    try:
        expansion = run(body, max_depth=128)
    except ExpansionLimit as limit:
        return ("limit", limit.what)
    return (serialize(expansion.root), sorted((p.name, p.message) for p in expansion.problems))


@pytest.mark.parametrize("levels", [10, 30, 60])
def test_where_a_bound_is_met_depends_on_the_document_not_the_stack(levels: int) -> None:
    deep = "-" * 30 + "1"
    chain = '<xacro:property name="p0" value="1"/>' + "".join(
        f'<xacro:property name="p{n}" value="${{p{n - 1} + {deep}}}"/>' for n in range(1, 32)
    )
    nest = (
        '<xacro:macro name="nest" params="k"><a><xacro:if value="${k > 0}">'
        '<xacro:nest k="${k - 1}"/></xacro:if><xacro:unless value="${k > 0}">'
        '<b v="${p31}" w="${p2}"/></xacro:unless></a></xacro:macro>'
    )
    body = chain + nest + f'<xacro:nest k="{levels}"/>'
    outcomes = [at_depth(frames, lambda: outcome(body)) for frames in (0, 100, 200)]
    assert outcomes[0] == outcomes[1] == outcomes[2]
