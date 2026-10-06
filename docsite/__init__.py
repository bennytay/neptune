"""The product documentation site (Linear MVL-191; packages/neptune-platform ADR 0012).

Sphinx + MyST + Furo, built offline and in strict mode by ``make docs`` (part of ``make check``).
Nothing here copies a document by hand: the build assembles a source tree whose paths mirror the
repository (``docs/architecture.md`` is the page ``docs/architecture``), so links between included
documents work unchanged, rewrites a link to any other repository file to its GitHub URL, fails on
a link to a file that does not exist, and adds the generated pages.

- ``sources``: what the site is made of (sections, API modules, the repository URL).
- ``links``: finding and rewriting a Markdown document's links.
- ``schemas``: rendering each published contract version's JSON Schema as a page.
- ``assemble``: the source tree: mirrored documents, hand-written ``pages/``, generated pages.
- ``build``: assemble, run Sphinx with warnings as errors, check the output loads nothing remote.

``python -m docsite`` builds into ``build/docs/html``; Platform's tests cover the pieces.
"""
