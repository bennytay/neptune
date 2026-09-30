import pytest

from neptune.discovery.scan import _covered


@pytest.mark.parametrize(
    ("raw", "blind", "links", "covered"),
    [
        (b"a/b", set(), set(), True),
        (b"a/b", {b"a/b"}, set(), False),
        (b"a/b", {b"a"}, set(), False),
        (b"a/b/c", {b"a"}, set(), False),
        (b"a/b", {b"."}, set(), False),
        (b"a/b", set(), {b"a"}, False),
        (b"a/b", set(), {b"a/b"}, True),  # a symlink *at* the location means the file is gone
        (b"ab/c", {b"a"}, {b"a"}, True),  # prefix of a name is not an ancestor
    ],
)
def test_covered(raw: bytes, blind: set[bytes], links: set[bytes], covered: bool) -> None:
    assert _covered(raw, blind, links) is covered
