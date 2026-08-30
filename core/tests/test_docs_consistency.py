# SPDX-License-Identifier: BUSL-1.1
"""Guards that keep the documentation set honest.

Three kinds of rot were found when the implementation state was frozen on
2026-08-30, each of them invisible to the existing suite:

1. **The index went stale.** ``docs/README.md`` is the role-based entry point
   into the whole documentation, but ten concept documents added over the
   summer were never listed there. A reader looking for the escalation or the
   simulation concept simply could not find it from the index.
2. **The published sync whitelist drifted.** ``docs/README.md`` repeats which
   files are mirrored into the public customer repository. Two operations
   guides had been added to the workflow but not to that list, so the document
   promised less than the automation actually publishes.
3. **Special characters were destroyed.** At some point an editing round-trip
   replaced ``->``-style arrows and set symbols with literal question marks in
   several documents (the architecture concept alone lost 36 of them,
   including the whole repository tree drawing). A space-delimited ``?`` never
   occurs in correct German or English typography, which makes it a reliable
   signature of exactly that damage.

These checks are pure file-system reads (no Markdown renderer, no network) and
therefore run anywhere the rest of the suite runs.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCS = _REPO_ROOT / "docs"
_INDEX = _DOCS / "README.md"
_SYNC_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "sync-customer-repo.yml"

# Markdown files that are checked for encoding damage. Deliberately explicit
# instead of a repository-wide walk: the working tree also holds ignored
# directories (virtual environments, the marketing site, private notes) whose
# content is none of this suite's business.
_MARKDOWN_ROOTS = (
    _REPO_ROOT,
    _DOCS,
    _REPO_ROOT / "core",
    _REPO_ROOT / "deploy",
    _REPO_ROOT / "deploy" / "demo",
)


def _markdown_files() -> list[Path]:
    """Collect the tracked Markdown files, without descending into ignored trees."""

    found: dict[Path, None] = {}
    for root in _MARKDOWN_ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            found[path] = None
    return list(found)


def _index_text() -> str:
    return _INDEX.read_text(encoding="utf-8")


def test_every_documentation_file_is_listed_in_the_index() -> None:
    """No document may hide from ``docs/README.md``.

    A concept document that nobody can reach from the index is, for practical
    purposes, lost -- the knowledge exists but is not findable.
    """

    text = _index_text()
    missing = [
        path.name
        for path in sorted(_DOCS.glob("*.md"))
        if path.name != "README.md" and f"({path.name})" not in text
    ]
    assert not missing, (
        "not linked from docs/README.md: "
        + ", ".join(missing)
        + " -- add a row to the matching table"
    )


def test_index_links_to_documents_resolve() -> None:
    """Every relative Markdown link in the index must point at a real file."""

    text = _index_text()
    broken = []
    for target in re.findall(r"\]\(([^)#]+\.md)(?:#[^)]*)?\)", text):
        if target.startswith(("http://", "https://")):
            continue
        if not (_DOCS / target).resolve().is_file():
            broken.append(target)
    assert not broken, "dead links in docs/README.md: " + ", ".join(sorted(set(broken)))


def test_index_repeats_the_sync_whitelist_completely() -> None:
    """The whitelist quoted in the index must match the workflow that runs it.

    ``sync-customer-repo.yml`` is the single source of truth for what reaches
    the public customer repository. Whenever the index repeats that list it
    must repeat it in full, otherwise the documentation understates what is
    published -- which is the dangerous direction for a repository split.
    """

    workflow = _SYNC_WORKFLOW.read_text(encoding="utf-8")
    copied = set(re.findall(r"cp source/(docs/[A-Za-z0-9_.-]+\.md)", workflow))
    assert copied, "no docs are copied by the sync workflow -- did its syntax change?"

    text = _index_text()
    missing = sorted(name for name in copied if f"`{name}`" not in text)
    assert not missing, (
        "docs/README.md does not mention these mirrored files: " + ", ".join(missing)
    )


def test_no_markdown_carries_symbols_lost_to_an_encoding_round_trip() -> None:
    """A space-delimited ``?`` is the signature of a destroyed arrow or symbol.

    German and English typography never puts a blank before a question mark, so
    the pattern cannot occur in intact prose. Code fences are not exempt: the
    damage hit table cells, Mermaid labels and a directory tree alike.
    """

    offenders = []
    for path in _markdown_files():
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if " ? " in line:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{number}")
    assert not offenders, (
        "lost special characters (restore the intended arrow/symbol): "
        + ", ".join(offenders)
    )
