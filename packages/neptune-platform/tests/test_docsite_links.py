"""The site's link handling: finding links by parsing, resolving them, rewriting them as written."""

from typing import Final

import pytest
from docsite import links

REPO: Final = "https://github.com/o/r"
FILES: Final = {"docs/a.md", "src/x.py", "docs/img.png", "README.md"}
DIRS: Final = {"docs", "src", "packages/p"}
PAGES: Final = frozenset({"docs/a.md", "docs/b.md", "concepts/c.md"})


def _kind(path: str) -> links.PathKind:
    return "file" if path in FILES else "dir" if path in DIRS else None


def _resolve(target: str, page: str = "docs/b.md", *, image: bool = False) -> links.Resolution:
    return links.resolve(
        target, page, image=image, pages=PAGES, kind=_kind, repository=REPO, ref="main"
    )


def test_find_reads_links_images_and_definitions_but_not_code() -> None:
    text = (
        "See [a](a.md) and ![pic](img.png).\n\n"
        "`[not](code.md)` and\n\n```\n[nor](fence.md)\n```\n\n"
        "[ref]: ../src/x.py\n\nUse [ref] twice: [again](a.md).\n"
    )
    assert links.find(text) == [
        links.Found("a.md", image=False),
        links.Found("img.png", image=True),
        links.Found("../src/x.py", image=False),
    ]


def test_find_reports_destinations_as_written() -> None:
    assert links.find("[x](a%20b.md) [y](<c d.md>)") == [
        links.Found("a%20b.md", image=False),
        links.Found("c d.md", image=False),
    ]


@pytest.mark.parametrize(
    "target",
    ["https://example.com/x", "mailto:a@b.c", "neptune://claim/1", "#local", "//cdn/x", "", "a.md"],
)
def test_external_anchors_and_pages_are_kept(target: str) -> None:
    assert _resolve(target) == links.Keep()


def test_a_page_with_a_fragment_is_kept_for_sphinx_to_check() -> None:
    assert _resolve("a.md#some-heading") == links.Keep()
    assert _resolve("../docs/b.md", page="concepts/c.md") == links.Keep()


def test_a_repository_file_or_directory_becomes_its_github_url() -> None:
    assert _resolve("../src/x.py#L3") == links.Rewrite(f"{REPO}/blob/main/src/x.py#L3")
    assert _resolve("/README.md") == links.Rewrite(f"{REPO}/blob/main/README.md")
    assert _resolve("../packages/p/") == links.Rewrite(f"{REPO}/tree/main/packages/p")
    assert _resolve("..") == links.Rewrite(f"{REPO}/tree/main")


def test_missing_and_escaping_targets_are_broken() -> None:
    missing = _resolve("nope.md")
    assert isinstance(missing, links.Broken) and "docs/nope.md" in missing.reason
    escaping = _resolve("../../outside.md")
    assert isinstance(escaping, links.Broken) and "leaves the repository" in escaping.reason


def test_an_image_is_copied_not_linked_remotely() -> None:
    assert _resolve("img.png", image=True) == links.Copy("docs/img.png")
    assert isinstance(_resolve("../src", image=True), links.Broken)


def test_rewrite_replaces_inline_titled_angled_and_reference_forms() -> None:
    text = (
        '[a](../src/x.py) [b](../src/x.py "t") [c](<../src/x.py>)\n'
        "[d]: ../src/x.py\n"
        "[e](../src/x.pyc) stays\n"
    )
    out, missing = links.rewrite(text, {"../src/x.py": "U"})
    assert missing == []
    assert out == '[a](U) [b](U "t") [c](<U>)\n[d]: U\n[e](../src/x.pyc) stays\n'


def test_rewrite_reports_a_destination_it_cannot_find_as_written() -> None:
    text = "[a](../src/x\\_y.py)\n"
    out, missing = links.rewrite(text, {"../src/x_y.py": "U"})
    assert out == text
    assert missing == ["../src/x_y.py"]
