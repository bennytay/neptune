"""How the record source treats a feed's own statements: repeats, removals, partial pages and
oversize names (ADR 0008 §5 and §8), with a hand-made system so each case is exact."""

import time
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deploy_records_fake import (
    FakeServer,
    ServiceNowBackend,
)
from deploy_records_fake_graph import LinearBackend
from deploy_records_support import codes, linear, online, servicenow
from neptune_deploy.sources.records import RecordSource
from neptune_deploy.sources.records.config import Location, Options
from neptune_deploy.sources.records.model import Fetch, Item, Page, safe_name

if TYPE_CHECKING:
    from neptune.model.jsonvalue import JsonValue


class Scripted:
    """A system that yields the pages it was given."""

    def __init__(self, *pages: Page) -> None:
        self.api: Any = None
        self.declared_options: dict[str, dict[str, JsonValue]] = {}
        self._pages = pages

    def pages(self, since: str | None) -> Generator[Page, None, None]:
        yield from self._pages

    def download(self, fetch: Fetch) -> bytes:
        raise NotImplementedError


def item(item_id: str, token: str, *, later_wins: bool = False) -> Item:
    body = f"{item_id}@{token}".encode()
    return Item(item_id, token, f"{item_id}.txt", len(body), body=body, later_wins=later_wins)


def source(tmp_path: Path, *pages: Page) -> RecordSource:
    return RecordSource(
        Location("deploy_test", "@x", "scope"), Scripted(*pages), online(tmp_path), Options(), {}
    )


def test_an_id_stated_twice_then_removed_is_removed_and_the_listing_survives(
    tmp_path: Path,
) -> None:
    pages = (
        Page(items=(item("a/1", "v1"), item("a/2", "v1"), item("a/1", "v2"))),
        Page(removed=("a/1",)),
    )
    listing = source(tmp_path, *pages).listing()
    assert [e.id for e in listing.entries] == ["a/2"]
    assert listing.removed == ("a/1",)
    assert listing.complete


def test_a_parent_removed_takes_its_ambiguous_child_with_it(tmp_path: Path) -> None:
    child = Item("a/1/attachment/9", "t1", "x", 1, body=b"x", parent="a/1")
    other = Item("a/1/attachment/9", "t2", "x", 1, body=b"y", parent="a/1")
    listing = source(
        tmp_path, Page(items=(item("a/1", "v1"), child, other)), Page(removed=("a/1",))
    ).listing()
    assert listing.entries == () and listing.skipped == ()


def test_two_statements_of_one_id_are_ambiguous_unless_the_feed_is_ordered(
    tmp_path: Path,
) -> None:
    plain = source(tmp_path, Page(items=(item("a/1", "v1"), item("a/1", "v2"))))
    assert plain.listing().entries == ()
    assert codes(plain) == ["deploy_test.record_duplicated"]
    ordered = source(
        tmp_path,
        Page(items=(item("a/1", "v1", later_wins=True),)),
        Page(items=(item("a/1", "v2", later_wins=True),)),
    )
    assert [e.location.revision_token for e in ordered.listing().entries] == ["v2"]
    assert codes(ordered) == []


def test_a_partial_page_leaves_no_cursor_even_if_a_later_page_names_one(tmp_path: Path) -> None:
    listing = source(
        tmp_path,
        Page(items=(item("a/1", "v1"),), cursor="next", partial=True),
        Page(items=(item("a/2", "v1"),), resume="deploy_test/1:end"),
    ).listing()
    assert not listing.complete and listing.cursor is None


def test_an_oversize_name_is_cut_once_whatever_its_size() -> None:
    start = time.monotonic()
    name = safe_name("é" * 3_000_000 + ".pdf", "fallback")
    assert time.monotonic() - start < 1.0
    assert len(name.encode("utf-8")) <= 255 and name.endswith(".pdf")
    assert safe_name("x" * 400, "f") == "x" * 255
    assert safe_name("a" * 300 + "." + "y" * 16, "f").endswith("." + "y" * 16)


def test_a_servicenow_value_with_no_deterministic_text_rejects_that_record_only(
    tmp_path: Path,
) -> None:
    backend = ServiceNowBackend()
    deep: Any = {"k": ["\ud800"]}  # an object cell is JSON text; a lone surrogate has none
    backend.rows[0]["short_description"] = deep
    with servicenow(FakeServer(backend), tmp_path) as source:
        listing = source.listing()
        assert listing.complete and len(listing.entries) == 2  # the other two records
        assert codes(source) == ["deploy_servicenow.record_unrepresentable"]


def test_a_linear_value_with_no_deterministic_text_rejects_that_issue_only(
    tmp_path: Path,
) -> None:
    backend = LinearBackend()
    deep: Any = {"k": ["\ud800"]}  # an object cell is JSON text; a lone surrogate has none
    backend.issues[0]["description"] = deep
    with linear(FakeServer(backend), tmp_path) as source:
        assert len(source.listing().entries) == 2
        assert codes(source) == ["deploy_linear.record_unrepresentable"]


def test_servicenow_attachments_are_asked_for_in_a_stable_order(tmp_path: Path) -> None:
    server = FakeServer(ServiceNowBackend())
    with servicenow(server, tmp_path) as source:
        source.listing()
    asked = [r.query["sysparm_query"] for r in server.requests("/api/now/attachment")]
    assert asked and all(q.endswith("^ORDERBYsys_id") for q in asked)
