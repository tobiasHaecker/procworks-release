# SPDX-License-Identifier: BUSL-1.1
"""Guards for the pinned CI dependencies (``core/requirements-ci.txt``).

``pyproject.toml`` only states lower bounds. Until 2026-09-25 the CI installed
the newest release of every dependency on each run, so an upstream release
could turn ``main`` red without any change in this repository (SQLAlchemy
2.1.0 did exactly that: ``func.max`` became ``Any`` and ``mypy --strict``
failed). The CI now installs the exact versions from the lock file.

A lock file only helps while it stays complete and actually used. Three ways
to lose that silently, each guarded here:

1. A dependency is added to ``pyproject.toml`` but not to the lock file. With
   ``--no-deps`` the CI would then fail at ``pip check`` -- but only a direct
   dependency is checked there, and an optional ``dev`` tool would simply be
   missing. The guard names the gap before the CI run does.
2. A line loosens to a range (``>=``) -- the file would no longer pin anything.
3. The workflow drops back to ``pip install -e ".[dev]"`` and ignores the file.

Text checks only, like the other pipeline guards. The workflow lives outside
``core/`` and is not shipped to the customer repository, so the workflow guard
skips there instead of failing a customer's test run.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parents[1]
_LOCK = _CORE / "requirements-ci.txt"
_PYPROJECT = _CORE / "pyproject.toml"
_CI_WORKFLOW = _CORE.parent / ".github" / "workflows" / "ci.yml"

#: A requirement's distribution name, up to the first extra/version/marker.
_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalize(name: str) -> str:
    """Return the PEP 503 normalized form of a distribution name.

    ``PyYAML``, ``pyyaml`` and ``py_yaml`` all name the same distribution; pip
    freeze writes whichever spelling the package uses, pyproject another one.

    :param name: a distribution name as written anywhere.
    :returns: lower-case name with runs of ``-``, ``_`` and ``.`` folded to ``-``.
    """

    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_pins() -> dict[str, str]:
    """Parse the lock file into ``{normalized name: version}``.

    Comment and blank lines are skipped. Every other line must be an exact pin
    ``name==version`` -- anything else fails the calling test with the line.

    :returns: mapping of normalized distribution name to pinned version.
    """

    pins: dict[str, str] = {}
    for line in _LOCK.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9.+!-]+)", entry)
        assert match, f"requirements-ci.txt: kein exakter Pin: {entry!r}"
        pins[_normalize(match.group(1))] = match.group(2)
    return pins


def _declared(requirements: list[str]) -> set[str]:
    """Return the normalized distribution names of pyproject requirement strings.

    :param requirements: entries such as ``"uvicorn[standard]>=0.29"``.
    :returns: the set of bare, normalized names (extras and bounds dropped).
    """

    names = set()
    for requirement in requirements:
        match = _NAME.match(requirement)
        assert match, f"pyproject.toml: unlesbare Abhängigkeit {requirement!r}"
        names.add(_normalize(match.group(1)))
    return names


def test_every_line_is_an_exact_pin() -> None:
    """The lock file pins exact versions and a meaningful number of packages."""

    pins = _lock_pins()
    # Direct plus transitive dependencies; a handful would mean a hand-written
    # list, not a frozen environment.
    assert len(pins) >= 20, pins


def test_lock_covers_runtime_and_dev_dependencies() -> None:
    """Every direct runtime and ``dev`` dependency of pyproject is pinned."""

    project = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]
    wanted = _declared(project["dependencies"]) | _declared(
        project["optional-dependencies"]["dev"]
    )
    # The license-scan job installs this tool at the same pinned version.
    wanted.add("pip-licenses")
    missing = sorted(wanted - _lock_pins().keys())
    assert not missing, (
        f"requirements-ci.txt fehlt {missing} -- neu erzeugen (Rezept im Dateikopf)"
    )


def test_lock_does_not_pin_procworks_itself() -> None:
    """The package under test is installed from the checkout, never from the lock."""

    assert "procworks" not in _lock_pins()


@pytest.mark.skipif(not _CI_WORKFLOW.exists(), reason="CI-Workflow nur im internen Repo")
def test_ci_installs_from_the_lock_file() -> None:
    """Both CI jobs take their versions from the lock file.

    The test job installs the lock, then procworks with ``--no-deps`` (so no
    unpinned resolution happens) and runs ``pip check``. The license scan uses
    the lock as constraints, which keeps it to runtime dependencies.
    """

    workflow = _CI_WORKFLOW.read_text(encoding="utf-8")
    assert "pip install -r requirements-ci.txt" in workflow
    assert "pip install --no-deps -e ." in workflow
    assert "pip check" in workflow
    assert "pip install -c requirements-ci.txt ." in workflow
    assert '".[dev]"' not in workflow, "Test-Job installiert wieder ungepinnt"
    assert "cache-dependency-path: core/pyproject.toml" not in workflow
