"""The site's source tree: mirrored documents, hand-written pages and generated pages.

``assemble(repo)`` returns every file of the tree by site path, in path order, plus the problems
found (broken links, paths claimed twice). It reads the repository and writes nothing; ``write``
puts a tree on disk. The same repository gives the same tree, byte for byte.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from docsite import links, schemas
from docsite.sources import (
    API_MODULES,
    EXTRA_DOCUMENTS,
    QUICKSTART_HEADING,
    REPOSITORY_REF,
    REPOSITORY_URL,
    SECTIONS,
    ApiModule,
    Section,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

PAGES = Path(__file__).resolve().parent / "pages"
QUICKSTART_MARKER = "<!-- docsite:readme-quickstart -->"
_H2 = re.compile(r"^## +(.+?)\s*#*\s*$")
_REGISTRY_ADR = "packages/neptune-platform/docs/adr/0002-contracts-registry-and-version-policy.md"
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass
class Tree:
    """The assembled source: site path -> bytes, and what is wrong with it."""

    files: dict[str, bytes] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def add(self, path: str, data: bytes, origin: str) -> None:
        if path in self.files:
            self.problems.append(f"{path}: claimed twice (again by {origin})")
            return
        self.files[path] = data


def _markdown(root: Path, directory: str) -> list[str]:
    base = root / directory
    found = (p.relative_to(root).as_posix() for p in base.rglob("*.md") if p.is_file())
    return sorted(found)


def section_documents(root: Path, section: Section) -> tuple[list[str], list[str], list[str]]:
    """A section's documents as repository paths: (guides, decisions, reviews), in site order."""
    paths = _markdown(root, section.root)

    def under(path: str, name: str) -> bool:
        return name in PurePosixPath(path).relative_to(section.root).parts[:-1]

    decisions = [p for p in paths if under(p, "adr")]
    reviews = [p for p in paths if under(p, "reviews") and p not in decisions]
    guides = [p for p in paths if p not in decisions and p not in reviews]
    lead = [f"{section.root}/{name}" for name in section.lead]
    guides = [p for p in lead if p in guides] + [p for p in guides if p not in lead]
    # an index (README) leads its directory's list
    decisions.sort(
        key=lambda p: (PurePosixPath(p).parent.as_posix(), PurePosixPath(p).stem != "README", p)
    )
    reviews.sort(key=lambda p: (PurePosixPath(p).stem != "README", p))
    return guides, decisions, reviews


def _docname(path: str) -> str:
    return "/" + path.removesuffix(".md")


def _toctree(entries: Iterable[str], *options: str) -> list[str]:
    return ["```{toctree}", *options, "", *entries, "```", ""]


def section_page(root: Path, section: Section) -> str:
    guides, decisions, reviews = section_documents(root, section)
    lines = [f"# {section.title}", "", section.summary, ""]
    lines += _toctree((_docname(p) for p in guides), ":maxdepth: 1")
    indexes = [p for p in decisions if PurePosixPath(p).stem == "README"]
    if decisions:
        lines += [
            "## Decisions",
            "",
            "Architecture decision records; the index lists every one.",
            "",
        ]
        lines += _toctree((_docname(p) for p in indexes), ":maxdepth: 1")
        rest = [p for p in decisions if p not in indexes]
        if rest:
            lines += _toctree((_docname(p) for p in rest), ":hidden:")
    if reviews:
        lines += ["## Gate reviews", ""]
        lines += _toctree((_docname(p) for p in reviews), ":maxdepth: 1")
    return "\n".join(lines).rstrip("\n") + "\n"


@dataclass(frozen=True)
class Contract:
    name: str
    meta: dict[str, object]
    versions: tuple[str, ...]  # directory names, newest first

    @property
    def owner(self) -> str:
        owner = self.meta.get("owner")
        return str(owner.get("package", "unknown")) if isinstance(owner, dict) else "unknown"


def contracts(root: Path) -> list[Contract]:
    out = []
    for path in sorted((root / "contracts").glob("*/contract.toml")):
        meta = tomllib.loads(path.read_text(encoding="utf-8"))
        versions = sorted(
            (p.parent.name for p in path.parent.glob("v*/schema.json")),
            key=schemas.version_key,
            reverse=True,
        )
        out.append(Contract(path.parent.name, meta, tuple(versions)))
    return out


def _owner_section(owner: str) -> Section | None:
    for section in SECTIONS:
        if section.root == f"packages/{owner}/docs" or (
            owner == "neptune" and section.root == "docs"
        ):
            return section
    return None


def contract_page(root: Path, contract: Contract) -> str:
    meta = contract.meta
    lines = [f"# {contract.name}", "", f"{schemas.escape(str(meta.get('title', '')))}", ""]
    facts = [
        ("Status", schemas.code(str(meta.get("status", "unknown")))),
        ("Owner", schemas.code(contract.owner)),
    ]
    consumers = meta.get("consumers")
    if isinstance(consumers, list) and consumers:
        facts.append(("Consumers", ", ".join(schemas.code(str(c)) for c in consumers)))
    lines += [f"- {name}: {value}" for name, value in facts]
    section = _owner_section(contract.owner)
    if section is not None:
        lines.append(f"- The owner's documents: [{section.title}](../../layers/{section.slug}.md)")
        owner_doc = f"{section.root}/contracts.md"
        if (root / owner_doc).is_file():
            lines.append(f"- The owner's contract notes: [contracts.md](../../{owner_doc})")
    lines += [
        "",
        "Published versions, newest first; each page renders that version's JSON Schema.",
        "",
    ]
    lines += _toctree((f"{v}/schema" for v in contract.versions), ":maxdepth: 1")
    return "\n".join(lines).rstrip("\n") + "\n"


def contracts_index(all_contracts: list[Contract]) -> str:
    published = [c for c in all_contracts if c.versions]
    pending = [c for c in all_contracts if not c.versions]
    lines = [
        "# Contracts and JSON Schemas",
        "",
        "A contract is what one layer publishes and others build against: a JSON Schema per",
        "released version, golden documents, and contract tests the owner runs. The registry lives",
        "in `contracts/`; the rules for versions and compatibility are Platform",
        f"[ADR 0002](../{_REGISTRY_ADR}).",
        "Every published version is rendered below; a consumer pins one in `contracts/lock.toml`.",
        "",
    ]
    lines += _toctree(
        ["/contracts/compatibility", *(f"{c.name}/index" for c in published)], ":maxdepth: 1"
    )
    if pending:
        lines += ["## Declared without a published schema", ""]
        for c in pending:
            note = f"part of `{c.meta['part_of']}`" if "part_of" in c.meta else "no export yet"
            status = c.meta.get("status", "unknown")
            title = schemas.escape(str(c.meta.get("title", "")))
            lines.append(f"- {schemas.code(c.name)} ({status}; {note}): {title}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def api_page(api: ApiModule) -> str:
    lines = [f"# {api.title}", "", f"{api.summary} Module {schemas.code(api.page)}.", ""]
    options = ["   :members:", "   :show-inheritance:"]
    if api.facade:
        options.append("   :imported-members:")
    # autodoc emits reStructuredText, so its directive runs inside eval-rst, not as MyST
    for module in api.modules:
        lines += ["```{eval-rst}", f".. automodule:: {module}", *options, "```", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def api_index() -> str:
    lines = [
        "# API reference",
        "",
        "Generated at build time from the public Python surfaces' docstrings and signatures.",
        "",
    ]
    lines += _toctree((m.page for m in API_MODULES), ":maxdepth: 1")
    return "\n".join(lines).rstrip("\n") + "\n"


def readme_section(readme: str, heading: str) -> str | None:
    """The body of the README's level-2 section whose heading starts with ``heading``."""
    out: list[str] | None = None
    fence: str | None = None
    for line in readme.splitlines():
        match = _FENCE.match(line)
        if fence is None and (h2 := _H2.match(line)):
            if out is not None:
                break
            if h2.group(1).strip().lower().startswith(heading):
                out = []
                continue
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
        if out is not None:
            out.append(line)
    return None if out is None else "\n".join(out).strip("\n") + "\n"


def quickstart(root: Path, template: str) -> str:
    readme = (root / "README.md").read_text(encoding="utf-8")
    body = readme_section(readme, QUICKSTART_HEADING)
    if body is None:
        body = (
            "The README does not carry its quickstart section yet; see the "
            "[repository README](README.md) for setup.\n"
        )
    return template.replace(QUICKSTART_MARKER, body.rstrip("\n"))


def _kind(root: Path) -> Callable[[str], links.PathKind]:
    def kind(path: str) -> links.PathKind:
        target = root / path
        if target.is_file():
            return "file"
        if target.is_dir():
            return "dir"
        return None

    return kind


def _fix_links(tree: Tree, root: Path, pages: frozenset[str]) -> None:
    kind = _kind(root)
    for path in sorted(p for p in tree.files if p.endswith(".md")):
        text = tree.files[path].decode("utf-8")
        replacements: dict[str, str] = {}
        for found in links.find(text):
            resolution = links.resolve(
                found.target,
                path,
                image=found.image,
                pages=pages,
                kind=kind,
                repository=REPOSITORY_URL,
                ref=REPOSITORY_REF,
            )
            if isinstance(resolution, links.Rewrite):
                replacements[found.target] = resolution.url
            elif isinstance(resolution, links.Copy):
                if resolution.path not in tree.files:
                    tree.files[resolution.path] = (root / resolution.path).read_bytes()
            elif isinstance(resolution, links.Broken):
                tree.problems.append(f"{path}: broken link: {resolution.reason}")
        if replacements:
            text, missing = links.rewrite(text, replacements)
            tree.problems += [f"{path}: cannot rewrite link {m!r} as written" for m in missing]
            tree.files[path] = text.encode("utf-8")


def assemble(root: Path, pages_dir: Path = PAGES) -> Tree:
    """The site's source tree for the repository at ``root``."""
    tree = Tree()
    for section in SECTIONS:
        guides, decisions, reviews = section_documents(root, section)
        for path in (*guides, *decisions, *reviews):
            tree.add(path, (root / path).read_bytes(), "a section")
        tree.add(f"layers/{section.slug}.md", section_page(root, section).encode(), "layers")
    for path in EXTRA_DOCUMENTS:
        tree.add(path, (root / path).read_bytes(), "EXTRA_DOCUMENTS")
    for page in sorted(p for p in pages_dir.rglob("*.md") if p.is_file()):
        site = page.relative_to(pages_dir).as_posix()
        text = page.read_text(encoding="utf-8")
        if QUICKSTART_MARKER in text:
            text = quickstart(root, text)
        tree.add(site, text.encode("utf-8"), "pages/")
    all_contracts = contracts(root)
    tree.add("contracts/index.md", contracts_index(all_contracts).encode(), "contracts")
    for contract in all_contracts:
        if not contract.versions:
            continue
        tree.add(
            f"contracts/{contract.name}/index.md",
            contract_page(root, contract).encode(),
            "contracts",
        )
        for version in contract.versions:
            base = f"contracts/{contract.name}/{version}"
            raw = (root / base / "schema.json").read_bytes()
            meta_path = root / base / "version.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
            try:
                text = schemas.render(
                    contract.name,
                    version.removeprefix("v"),
                    json.loads(raw),
                    meta,
                    download="schema.json",
                    owner=contract.owner,
                )
            except (ValueError, schemas.SchemaError) as error:
                tree.problems.append(f"{base}/schema.json: {error}")
                continue
            tree.add(f"{base}/schema.md", text.encode("utf-8"), "contracts")
            tree.add(f"{base}/schema.json", raw, "contracts")
    tree.add("api/index.md", api_index().encode(), "api")
    for api in API_MODULES:
        tree.add(f"api/{api.page}.md", api_page(api).encode(), "api")
    _fix_links(tree, root, frozenset(p for p in tree.files if p.endswith(".md")))
    tree.files = dict(sorted(tree.files.items()))
    return tree


def write(tree: Tree, out: Path) -> None:
    """Write ``tree`` under ``out``, which must be empty or absent."""
    for path, data in tree.files.items():
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
