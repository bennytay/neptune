"""SBOMs and container image indexes: identities a document states for other software (ADR 0040).

Each document is one ``SoftwareConfiguration``, ``stated``, with an item per package, component
or image it lists, in document order.

- **SPDX 2.x JSON**: each of ``packages``: ``name``, and ``versionInfo`` as ``release``
  (``NOASSERTION`` is the spec's "no assertion": ``Unknown`` citing it). A ``FIRMWARE`` package's
  release is a ``FirmwareVersion``, any other declared text.
- **CycloneDX JSON**: ``metadata.component``, then ``components`` with their nested
  ``components``, in pre-order: ``name`` and ``version``. A ``firmware`` component's release is a
  ``FirmwareVersion``. A ``machine-learning-model`` component's stated hash is its ``digest``
  (``ModelCheckpointHash``: its SHA-256 when it lists one, else its first hash of an algorithm the
  kind holds).
- **OCI image index / Docker manifest list** (``index.json``): each of ``manifests``: its
  ``digest`` (``ContainerImageDigest``), ``org.opencontainers.image.ref.name`` as ``name`` and
  ``org.opencontainers.image.version`` as ``release``. The VCS-agnostic
  ``org.opencontainers.image.revision`` is not a git commit by definition and stays in the bytes.

A package or component with an ``oci`` or ``docker`` package URL whose version is a digest is a
container image: that digest is its ``digest`` (cited as a ``Span`` of the URL). A container that
states none has ``digest`` ``Unknown``; for any other software ``digest`` is ``NotApplicable``.
"""

import re
import urllib.parse
from collections.abc import Iterator
from typing import Any, Final

from neptune.adapters.contract import SIGNATURE, STRUCTURE, VERIFIED, FormatSpec
from neptune.adapters.software._common import (
    Detected,
    Doc,
    Draft,
    Format,
    Reading,
    json_head,
    reason,
)
from neptune.model.knowledge import AssertionKind, Knowledge, Unknown
from neptune.model.machine import ArtifactDigest, Release
from neptune.model.provenance import ByteRange, EvidenceRef, Span
from neptune.model.versions import (
    ContainerImageDigest,
    FirmwareVersion,
    HashAlgorithm,
    ModelCheckpointHash,
)

_SPDX: Final = re.compile(rb'"spdxVersion"\s*:\s*"(SPDX-[0-9.]+)"')
_CYCLONEDX: Final = re.compile(rb'"bomFormat"\s*:\s*"CycloneDX"')
_CYCLONEDX_VERSION: Final = re.compile(rb'"specVersion"\s*:\s*"([0-9.]+)"')
_INDEX_TYPE: Final = re.compile(
    rb'"mediaType"\s*:\s*"application/vnd\.(?:oci\.image\.index\.v1|docker\.distribution\.'
    rb'manifest\.list\.v2)\+json"'
)
_SCHEMA_2: Final = re.compile(rb'"schemaVersion"\s*:\s*2\b')
_MANIFESTS: Final = re.compile(rb'"manifests"\s*:\s*\[')
_CONTAINER_PURLS: Final = frozenset({"oci", "docker"})
_HASHES: Final = {
    "MD5": HashAlgorithm.MD5,
    "SHA-1": HashAlgorithm.SHA1,
    "SHA-256": HashAlgorithm.SHA256,
    "SHA-384": HashAlgorithm.SHA384,
    "SHA-512": HashAlgorithm.SHA512,
}


def _json_object(head: bytes) -> bool:
    return head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{")


def _purl_digest(purl: object) -> tuple[str, int, int] | None:
    """An ``oci``/``docker`` package URL's digest version and its span in the URL, if any."""
    if not isinstance(purl, str) or not purl.startswith("pkg:"):
        return None
    kind = purl[4:].split("/", 1)[0].lower()
    if kind not in _CONTAINER_PURLS:
        return None
    end = min(i for i in (purl.find("?"), purl.find("#"), len(purl)) if i >= 0)
    at = purl.rfind("@", 0, end)
    if at < 0:
        return None
    version = urllib.parse.unquote(purl[at + 1 : end])
    if not version.startswith(("sha256:", "sha512:")):
        return None
    return version, at + 1, end


def _container_digest(
    reading: Reading, draft: Draft, purls: list[tuple[object, EvidenceRef]]
) -> Knowledge[ArtifactDigest] | None:
    for purl, at in purls:
        found = _purl_digest(purl)
        if found is not None:
            version, start, end = found
            spanned = reading.at(*at.locator, Span(start, end))
            digest: Knowledge[ArtifactDigest] = reading.value(
                draft, "digest", version, spanned, ContainerImageDigest, "OCI digest"
            )
            return digest
    return None


def _release(
    reading: Reading, draft: Draft, raw: object, at: EvidenceRef, firmware: bool
) -> Knowledge[Release]:
    if firmware:
        version: Knowledge[Release] = reading.value(
            draft, "release", raw, at, FirmwareVersion, "firmware version"
        )
        return version
    return reading.declared(draft, raw, at)


def _document(reading: Reading) -> tuple[Doc, dict[str, Any]] | None:
    data = reading.json(reading.document())
    if data is None:
        return None
    if not isinstance(data, dict):
        reading.malformed("is not a JSON object")
        return None
    return Doc(reading, (ByteRange(0, reading.source.size),)), data


def _limited(items: Iterator[Draft], limit: int) -> list[Draft]:
    drafts: list[Draft] = []
    for draft in items:
        drafts.append(draft)
        if len(drafts) > limit:
            break
    return drafts


# --- SPDX --------------------------------------------------------------------------------------


def _detect_spdx(head: bytes, size: int) -> Detected | None:
    match = _SPDX.search(head) if _json_object(head) else None
    if match is None:
        return None
    version = match[1].decode("ascii")
    document = json_head(head, size)
    if isinstance(document, dict) and isinstance(document.get("packages", []), list):
        return Detected(VERIFIED, reason("spdx", "an SPDX JSON document that parses"), version)
    return Detected(
        SIGNATURE, reason("spdx", "a JSON object with spdxVersion: an SPDX document"), version
    )


def _spdx_items(reading: Reading, doc: Doc, data: dict[str, Any]) -> Iterator[Draft]:
    packages = data.get("packages", [])
    if not isinstance(packages, list):
        reading.malformed("has packages that are not an array")
        return
    for index, package in enumerate(packages):
        at = doc.ref("packages", index)
        if not isinstance(package, dict):
            reading.malformed_entry(at, "is not an object")
            continue
        draft = Draft(entry=at)
        draft.name = reading.text(
            draft,
            "name",
            package.get("name"),
            doc.ref("packages", index, "name") if "name" in package else at,
        )
        version = package.get("versionInfo")
        version_at = doc.ref("packages", index, "versionInfo") if "versionInfo" in package else at
        purpose = package.get("primaryPackagePurpose")
        if version == "NOASSERTION":
            draft.release = Unknown(reading.provenance(version_at))
        else:
            draft.release = _release(reading, draft, version, version_at, purpose == "FIRMWARE")
        refs = package.get("externalRefs")
        purls = [
            (
                ref.get("referenceLocator"),
                doc.ref("packages", index, "externalRefs", n, "referenceLocator"),
            )
            for n, ref in enumerate(refs if isinstance(refs, list) else [])
            if isinstance(ref, dict) and ref.get("referenceType") == "purl"
        ]
        digest = _container_digest(reading, draft, purls)
        if digest is not None:
            draft.digest = digest
        elif purpose == "CONTAINER":
            draft.digest = Unknown(reading.provenance(at))
        yield draft


def _read_spdx(reading: Reading) -> list[Draft]:
    found = _document(reading)
    if found is None:
        return []
    doc, data = found
    return _limited(_spdx_items(reading, doc, data), reading.config.integer("max_items"))


# --- CycloneDX ---------------------------------------------------------------------------------


def _detect_cyclonedx(head: bytes, size: int) -> Detected | None:
    if not _json_object(head) or _CYCLONEDX.search(head) is None:
        return None
    found = _CYCLONEDX_VERSION.search(head)
    version = found[1].decode("ascii") if found else None
    document = json_head(head, size)
    if isinstance(document, dict) and document.get("bomFormat") == "CycloneDX":
        return Detected(VERIFIED, reason("cyclonedx", "a CycloneDX JSON BOM that parses"), version)
    return Detected(
        SIGNATURE, reason("cyclonedx", "a JSON object with bomFormat CycloneDX"), version
    )


def _model_digest(
    reading: Reading, doc: Doc, draft: Draft, path: tuple[str | int, ...], hashes: object
) -> Knowledge[ArtifactDigest] | None:
    listed = [
        (n, entry)
        for n, entry in enumerate(hashes if isinstance(hashes, list) else [])
        if isinstance(entry, dict) and entry.get("alg") in _HASHES
    ]
    if not listed:
        return None
    n, entry = next(((n, e) for n, e in listed if e.get("alg") == "SHA-256"), listed[0])
    algorithm = _HASHES[entry["alg"]]

    def kind(text: str) -> ModelCheckpointHash:
        return ModelCheckpointHash(algorithm, text)

    at = doc.ref(*path, "hashes", n, "content")
    digest: Knowledge[ArtifactDigest] = reading.value(
        draft, "digest", entry.get("content"), at, kind, f"{algorithm} digest"
    )
    return digest


def _components(
    reading: Reading, doc: Doc, data: dict[str, Any]
) -> Iterator[tuple[tuple[str | int, ...], dict[str, Any]]]:
    metadata = data.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    if isinstance(component, dict):
        yield ("metadata", "component"), component
    stack: list[tuple[tuple[str | int, ...], Iterator[tuple[int, Any]]]] = []
    if isinstance(data.get("components"), list):
        stack.append((("components",), enumerate(data["components"])))
    while stack:
        path, entries = stack[-1]
        following = next(entries, None)
        if following is None:
            stack.pop()
            continue
        index, entry = following
        if not isinstance(entry, dict):
            reading.malformed_entry(doc.ref(*path, index), "is not an object")
            continue
        yield (*path, index), entry
        if isinstance(entry.get("components"), list):
            stack.append(((*path, index, "components"), enumerate(entry["components"])))


def _cyclonedx_items(reading: Reading, doc: Doc, data: dict[str, Any]) -> Iterator[Draft]:
    for path, component in _components(reading, doc, data):
        at = doc.ref(*path)
        draft = Draft(entry=at)

        def where(
            key: str,
            path: tuple[str | int, ...] = path,
            component: dict[str, Any] = component,
            at: EvidenceRef = at,
        ) -> EvidenceRef:
            return doc.ref(*path, key) if key in component else at

        kind = component.get("type")
        draft.name = reading.text(draft, "name", component.get("name"), where("name"))
        draft.release = _release(
            reading, draft, component.get("version"), where("version"), kind == "firmware"
        )
        digest = _container_digest(reading, draft, [(component.get("purl"), where("purl"))])
        if digest is None and kind == "machine-learning-model":
            digest = _model_digest(reading, doc, draft, path, component.get("hashes"))
        if digest is not None:
            draft.digest = digest
        elif kind in ("container", "machine-learning-model"):
            draft.digest = Unknown(reading.provenance(at))
        yield draft


def _read_cyclonedx(reading: Reading) -> list[Draft]:
    found = _document(reading)
    if found is None:
        return []
    doc, data = found
    return _limited(_cyclonedx_items(reading, doc, data), reading.config.integer("max_items"))


# --- OCI image index ---------------------------------------------------------------------------


def _detect_oci_index(head: bytes, size: int) -> Detected | None:
    if not _json_object(head) or _SCHEMA_2.search(head) is None:
        return None
    document = json_head(head, size)
    if (
        isinstance(document, dict)
        and document.get("schemaVersion") == 2
        and isinstance(document.get("manifests"), list)
        and all(isinstance(m, dict) and "digest" in m for m in document["manifests"])
    ):
        return Detected(VERIFIED, reason("oci_index", "an image index that parses"))
    if _INDEX_TYPE.search(head):
        return Detected(SIGNATURE, reason("oci_index", "an image index's mediaType"))
    if _MANIFESTS.search(head):
        return Detected(STRUCTURE, reason("oci_index", "schemaVersion 2 with a manifests array"))
    return None


def _oci_items(reading: Reading, doc: Doc, data: dict[str, Any]) -> Iterator[Draft]:
    manifests = data.get("manifests", [])
    if not isinstance(manifests, list):
        reading.malformed("has manifests that are not an array")
        return
    for index, manifest in enumerate(manifests):
        at = doc.ref("manifests", index)
        if not isinstance(manifest, dict):
            reading.malformed_entry(at, "is not an object")
            continue
        draft = Draft(entry=at)
        digest_at = doc.ref("manifests", index, "digest") if "digest" in manifest else at
        draft.digest = reading.value(
            draft, "digest", manifest.get("digest"), digest_at, ContainerImageDigest, "OCI digest"
        )
        annotations = manifest.get("annotations")
        notes = annotations if isinstance(annotations, dict) else {}
        for key, field, read in (
            ("org.opencontainers.image.ref.name", "name", _name_reader),
            ("org.opencontainers.image.version", "release", _version_reader),
        ):
            where = doc.ref("manifests", index, "annotations", key) if key in notes else at
            setattr(draft, field, read(reading, draft, notes.get(key), where))
        yield draft


def _name_reader(reading: Reading, draft: Draft, raw: object, at: EvidenceRef) -> Knowledge[str]:
    return reading.text(draft, "name", raw, at)


def _version_reader(
    reading: Reading, draft: Draft, raw: object, at: EvidenceRef
) -> Knowledge[Release]:
    return reading.declared(draft, raw, at)


def _read_oci_index(reading: Reading) -> list[Draft]:
    found = _document(reading)
    if found is None:
        return []
    doc, data = found
    return _limited(_oci_items(reading, doc, data), reading.config.integer("max_items"))


SPDX: Final = Format(
    key="spdx",
    label="SPDX document",
    spec=FormatSpec("SPDX 2 SBOM (JSON)", extensions=(".spdx.json",)),
    assertion=AssertionKind.STATED,
    detect=_detect_spdx,
    read=_read_spdx,
)
CYCLONEDX: Final = Format(
    key="cyclonedx",
    label="CycloneDX BOM",
    spec=FormatSpec("CycloneDX SBOM (JSON)", extensions=(".cdx.json",)),
    assertion=AssertionKind.STATED,
    detect=_detect_cyclonedx,
    read=_read_cyclonedx,
)
OCI_INDEX: Final = Format(
    key="oci_index",
    label="image index",
    spec=FormatSpec(
        "OCI image index or Docker manifest list",
        media_types=(
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.index.v1+json",
        ),
    ),
    assertion=AssertionKind.STATED,
    detect=_detect_oci_index,
    read=_read_oci_index,
)
