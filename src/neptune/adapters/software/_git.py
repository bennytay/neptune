"""Git refs, read as git writes them, without running git or reading the object store (ADR 0040).

- A **ref file** (``HEAD``, ``ORIG_HEAD``, a loose ``refs/heads/<branch>``) is one line: an object
  name, or ``ref: <refname>`` naming another ref. An object name is the item's ``commit``
  (``observed``: git's own record of where the ref points); the item has no name, because a loose
  ref's name is its path, which is the source's location. A symbolic ref names no commit: no
  record, and ``software.git_symbolic_ref`` cites the ref it names, for binding (MVL-38) to follow.
- **packed-refs** holds one ref per line, ``<object name> <refname>``, an annotated tag followed
  by ``^<object name>``, the commit it peels to. Each ref is an item: ``name`` is the refname,
  ``commit`` the peeled object name when there is one, else the object name. In a file whose
  header does not say it peels its tags (``peeled`` or ``fully-peeled``), a tag without a ``^``
  line may name a tag object: its commit is ``Unknown`` with ``software.git_unpeeled_tag``.

Resolving ``HEAD`` to a commit combines several files and is binding's (MVL-38). Dirty state needs
the working tree as well as the index, so nothing here claims it.
"""

import io
import re
from dataclasses import dataclass
from typing import Final

from neptune.adapters.contract import SIGNATURE, STRUCTURE, FormatSpec
from neptune.adapters.software._common import Detected, Draft, Format, Reading, reason
from neptune.model.finding import FindingCategory, Severity
from neptune.model.knowledge import AssertionKind, Known, Unknown
from neptune.model.versions import GitCommit

# A ref file is one short line; anything longer is not one.
MAX_REF_FILE: Final = 1024
_OBJECT: Final = rb"[0-9a-f]{40}(?:[0-9a-f]{24})?"
# git check-ref-format forbids controls, space and ~^:?*[\ in refnames.
_REFNAME: Final = rb"refs/[^\x00-\x20\x7f~^:?*\[\\]+"
_REF_FILE: Final = re.compile(
    rb"(?:ref: (?P<target>" + _REFNAME + rb")|(?P<sha>" + _OBJECT + rb"))\n?"
)
_PACKED_HEADER: Final = b"# pack-refs with:"
_PACKED_REF: Final = re.compile(rb"(?P<sha>" + _OBJECT + rb") (?P<name>" + _REFNAME + rb")")
_PACKED_PEEL: Final = re.compile(rb"\^(?P<sha>" + _OBJECT + rb")")
# Names that say a one-line hex file is a checksum, not a ref (the bytes cannot tell them apart).
CHECKSUM_SUFFIXES: Final = (".md5", ".sha1", ".sha256", ".sha512", ".sum", ".checksum", ".hash")


def _detect_ref(head: bytes, size: int) -> Detected | None:
    if size > MAX_REF_FILE or len(head) != size:
        return None
    match = _REF_FILE.fullmatch(head)
    if match is None:
        return None
    if match["target"] is not None:
        return Detected(STRUCTURE, reason("git_symbolic_ref", "one line naming a ref: a git ref"))
    return Detected(STRUCTURE, reason("git_ref", "one line holding a git object name: a git ref"))


def _read_ref(reading: Reading) -> list[Draft]:
    data = reading.document()
    match = None if data is None else _REF_FILE.fullmatch(data)
    if match is None:
        if data is not None:
            reading.malformed("is not one line holding an object name or a ref")
        return []
    if match["target"] is not None:
        start, end = match.span("target")
        target = match["target"].decode("ascii", errors="replace")
        reading.report(
            "git_symbolic_ref",
            FindingCategory.MISSING,
            Severity.INFO,
            reading.span(start, end - start),
            "the ref names another ref, not a commit; binding follows it to the commit",
            {"target": target},
        )
        return []
    start, end = match.span("sha")
    at = reading.span(start, end - start)
    draft = Draft(entry=at)
    draft.commit = Known(GitCommit(match["sha"].decode("ascii")), reading.provenance(at))
    return [draft]


def _detect_packed(head: bytes, size: int) -> Detected | None:
    if head.startswith(_PACKED_HEADER):
        return Detected(SIGNATURE, reason("git_packed_refs", "starts with git's pack-refs header"))
    first = head.split(b"\n", 1)[0]
    if _PACKED_REF.fullmatch(first):
        return Detected(STRUCTURE, reason("git_packed_refs", "a line of git packed-refs"))
    return None


@dataclass
class _Ref:
    """A packed ref read so far: its line (and peel line) ``[start, end)`` and its object name."""

    draft: Draft
    start: int
    end: int
    sha: bytes
    sha_at: int
    tag: bool


def _read_packed(reading: Reading) -> list[Draft]:
    data = reading.document()
    if data is None:
        return []
    drafts: list[Draft] = []
    peeled = False
    pending: _Ref | None = None
    skipped = False  # the last ref line was skipped, so its peel line is too

    def close() -> None:
        nonlocal pending
        if pending is None:
            return
        ref, draft = pending, pending.draft
        draft.entry = reading.span(ref.start, ref.end - ref.start)
        if ref.tag and not peeled:
            draft.explained.add("commit")
            draft.commit = Unknown(reading.provenance(draft.entry))
            reading.report(
                "git_unpeeled_tag",
                FindingCategory.AMBIGUOUS,
                Severity.WARNING,
                draft.entry,
                "a tag in packed-refs that does not say it peels its tags may name a tag object,"
                " not a commit; its commit is unknown",
                about_record=True,
            )
        else:
            at = reading.span(ref.sha_at, len(ref.sha))
            draft.commit = Known(GitCommit(ref.sha.decode("ascii")), reading.provenance(at))
        drafts.append(draft)
        pending = None

    offset = 0
    for line in io.BytesIO(data):  # lazily: a file of empty lines is not a list of them
        if reading.full(drafts):
            break
        start, content = offset, line.rstrip(b"\r\n")
        offset += len(line)
        end = start + len(content)
        if start == 0 and content.startswith(_PACKED_HEADER):
            traits = content[len(_PACKED_HEADER) :].split()
            peeled = b"peeled" in traits or b"fully-peeled" in traits
            continue
        ref, peel = _PACKED_REF.fullmatch(content), _PACKED_PEEL.fullmatch(content)
        if ref is not None:
            close()
            name_start, name_end = ref.span("name")
            name_at = reading.span(start + name_start, name_end - name_start)
            try:  # git allows any non-control bytes in a refname; only UTF-8 is a text value
                name = ref["name"].decode("utf-8")
            except UnicodeDecodeError:
                reading.malformed_entry(name_at, "has a ref name that is not UTF-8")
                skipped = True
                continue
            skipped = False
            draft = Draft(entry=reading.span(start, len(content)))
            draft.name = Known(name, reading.provenance(name_at))
            tag = ref["name"].startswith(b"refs/tags/")
            pending = _Ref(draft, start, end, ref["sha"], start + ref.start("sha"), tag)
        elif peel is not None and skipped:
            skipped = False  # the peel line of a ref that was skipped
        elif peel is not None and pending is not None:
            pending.end, pending.tag = end, False
            pending.sha, pending.sha_at = peel["sha"], start + peel.start("sha")
            close()
        elif content:
            close()
            skipped = False
            reading.malformed_entry(
                reading.span(start, len(content)), "is not a ref or a peel line"
            )
    close()
    return drafts


GIT_REF: Final = Format(
    key="git_ref",
    label="git ref",
    spec=FormatSpec("Git ref file (HEAD, refs/*)"),
    assertion=AssertionKind.OBSERVED,
    detect=_detect_ref,
    read=_read_ref,
)

GIT_PACKED_REFS: Final = Format(
    key="git_packed_refs",
    label="git packed-refs",
    spec=FormatSpec("Git packed-refs"),
    assertion=AssertionKind.OBSERVED,
    detect=_detect_packed,
    read=_read_packed,
)
