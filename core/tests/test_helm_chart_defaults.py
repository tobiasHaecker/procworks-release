# SPDX-License-Identifier: BUSL-1.1
"""Guards for the defaults of the Helm chart (``deploy/helm/``).

Until 1.30.0 the chart shipped with placeholders: ``values.yaml`` had
``image.repository: your-org/procworks`` and ``Chart.yaml`` pointed ``home`` /
``sources`` at ``github.com/your-org/procworks``. A ``helm install`` without
overrides therefore tried to pull ``ghcr.io/your-org/procworks-api`` -- an image
that does not exist -- and failed.

The default has to pull the published release images
``ghcr.io/tobiashaecker/procworks-api`` / ``-web`` at the chart ``appVersion``.
Registry paths are lowercase only (OCI distribution spec), so the GitHub owner
is written in lowercase there. The defaults live in ``image.apiRepository`` /
``image.webRepository``; ``image.repository`` stays as an optional common
prefix (``<repository>-api`` / ``-web``) so existing overrides keep working.

There is no ``helm`` binary in CI, so the image reference is rebuilt in Python
from the same inputs the template uses: ``_helpers.tpl`` is checked to still
compose the image this way, and the values are read from ``values.yaml`` /
``Chart.yaml``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml")

_HELM_DIR = Path(__file__).resolve().parents[2] / "deploy" / "helm"
_CHART = _HELM_DIR / "Chart.yaml"
_VALUES = _HELM_DIR / "values.yaml"
_HELPERS = _HELM_DIR / "templates" / "_helpers.tpl"

pytestmark = pytest.mark.skipif(
    not (_CHART.exists() and _VALUES.exists() and _HELPERS.exists()),
    reason="the Helm chart is not part of this checkout",
)

#: The published images (without tag) and where customers find the chart.
_EXPECTED_IMAGES = {
    "api": "ghcr.io/tobiashaecker/procworks-api",
    "web": "ghcr.io/tobiashaecker/procworks-web",
}
_PUBLIC_REPO = "https://github.com/tobiasHaecker/procworks-release"

#: Fragments that mark an unfilled placeholder in a default value.
_PLACEHOLDERS = ("your-org", "example", "changeme", "<")

#: Value keys under ``image`` that form the image path.
_PATH_FIELDS = ("registry", "repository", "apiRepository", "webRepository")


def _load(path: Path) -> dict[str, Any]:
    """Parse a YAML file of the chart into a mapping.

    Args:
        path: ``Chart.yaml`` or ``values.yaml``.

    Returns:
        The parsed top-level mapping.
    """

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} is not a mapping"
    return data


def default_images(chart: dict[str, Any], values: dict[str, Any], helpers: str) -> dict[str, str]:
    """Render the image references the chart uses for the given values.

    Mirrors ``procworks.api.image`` / ``procworks.web.image`` in
    ``_helpers.tpl``: ``<registry>/<path>:<tag>`` with
    ``path = <repository>-<component>`` if ``image.repository`` is non-empty,
    else ``image.<component>Repository``, and
    ``tag = image.<component>Tag | default .Chart.AppVersion``.

    Args:
        chart: Parsed ``Chart.yaml``.
        values: Parsed ``values.yaml`` (possibly with overrides applied).
        helpers: Text of ``templates/_helpers.tpl``.

    Returns:
        ``{"api": <image>, "web": <image>}``.

    Raises:
        AssertionError: when ``_helpers.tpl`` no longer composes the image the
            way this function mirrors -- then the mirror must be updated
            together with the template, not silently diverge.
    """

    image = values["image"]
    rendered: dict[str, str] = {}
    for component in ("api", "web"):
        expected_fragments = (
            rf"\$tag := \.Values\.image\.{component}Tag \| default \.Chart\.AppVersion",
            rf"\$path := \.Values\.image\.{component}Repository",
            r"if \.Values\.image\.repository",
            rf'\$path = printf "%s-{component}" \.Values\.image\.repository',
            r'printf "%s/%s:%s" \.Values\.image\.registry \$path \$tag',
        )
        # The define runs up to the next one (its own ``end`` is ambiguous:
        # the ``if`` inside ends the same way).
        block = re.search(
            rf'define "procworks\.{component}\.image".*?(?=define "|\Z)', helpers, re.S
        )
        assert block is not None and all(
            re.search(fragment, block.group(0)) for fragment in expected_fragments
        ), f"_helpers.tpl composes the {component} image differently than this test mirrors"
        prefix = image.get("repository") or ""
        path = f"{prefix}-{component}" if prefix else image.get(f"{component}Repository")
        tag = image.get(f"{component}Tag") or chart["appVersion"]
        rendered[component] = f"{image['registry']}/{path}:{tag}"
    return rendered


def default_problems(chart: dict[str, Any], values: dict[str, Any], helpers: str) -> list[str]:
    """List every way a default ``helm install`` would pull the wrong image.

    Checks:

    * ``image.registry`` / ``repository`` / ``apiRepository`` /
      ``webRepository`` contain no placeholder and are lowercase (registry
      paths reject uppercase);
    * the rendered API and web images are the published ones
      (:data:`_EXPECTED_IMAGES`) at ``appVersion``;
    * ``home`` and every ``sources`` entry point to the public release repo.

    Args:
        chart: Parsed ``Chart.yaml``.
        values: Parsed ``values.yaml``.
        helpers: Text of ``templates/_helpers.tpl``.

    Returns:
        Human-readable problems, each naming the offending field; empty when
        the defaults are correct.
    """

    problems: list[str] = []
    image = values.get("image") or {}
    for field in _PATH_FIELDS:
        value = str(image.get(field) or "")
        if any(marker in value.lower() for marker in _PLACEHOLDERS):
            problems.append(f"values.yaml image.{field} is a placeholder: '{value}'")
        if value != value.lower():
            problems.append(f"values.yaml image.{field} is not lowercase: '{value}'")
    if not problems:
        app_version = chart.get("appVersion")
        for component, ref in default_images(chart, values, helpers).items():
            expected = f"{_EXPECTED_IMAGES[component]}:{app_version}"
            if ref != expected:
                problems.append(f"default {component} image is '{ref}', expected '{expected}'")
    links = {"home": [chart.get("home")], "sources": chart.get("sources") or [None]}
    for field, urls in links.items():
        for url in urls:
            if url != _PUBLIC_REPO:
                problems.append(f"Chart.yaml {field} is '{url}', expected '{_PUBLIC_REPO}'")
    return problems


def test_default_install_pulls_the_published_images() -> None:
    """Without overrides the chart renders the published ghcr.io images."""

    chart, values = _load(_CHART), _load(_VALUES)
    helpers = _HELPERS.read_text(encoding="utf-8")
    problems = default_problems(chart, values, helpers)
    assert not problems, "Helm chart defaults are wrong:\n" + "\n".join(problems)
    assert default_images(chart, values, helpers) == {
        "api": f"ghcr.io/tobiashaecker/procworks-api:{chart['appVersion']}",
        "web": f"ghcr.io/tobiashaecker/procworks-web:{chart['appVersion']}",
    }


def test_common_prefix_override_still_works() -> None:
    """``image.repository`` as before: ``<repository>-api`` / ``-web`` for both."""

    chart, values = _load(_CHART), _load(_VALUES)
    values["image"].update(registry="registry.local:5000", repository="my-org/procworks")
    images = default_images(chart, values, _HELPERS.read_text(encoding="utf-8"))
    assert images == {
        "api": f"registry.local:5000/my-org/procworks-api:{chart['appVersion']}",
        "web": f"registry.local:5000/my-org/procworks-web:{chart['appVersion']}",
    }


@pytest.mark.parametrize(
    ("target", "field", "value", "reason"),
    [
        ("values", "repository", "your-org/procworks", "image.repository is a placeholder"),
        (
            "values",
            "apiRepository",
            "tobiasHaecker/procworks-api",
            "image.apiRepository is not lowercase",
        ),
        (
            "values",
            "webRepository",
            "tobiashaecker/other-web",
            "default web image is 'ghcr.io/tobiashaecker/other-web:",
        ),
        (
            "values",
            "repository",
            "tobiashaecker/other",
            "default api image is 'ghcr.io/tobiashaecker/other-api:",
        ),
        ("values", "registry", "docker.io", "default web image is 'docker.io/"),
        (
            "values",
            "apiTag",
            "latest",
            "default api image is 'ghcr.io/tobiashaecker/procworks-api:latest'",
        ),
        ("chart", "home", "https://github.com/your-org/procworks", "Chart.yaml home is"),
        (
            "chart",
            "sources",
            ["https://github.com/your-org/procworks"],
            "Chart.yaml sources is 'https://github.com/your-org/procworks'",
        ),
    ],
)
def test_default_check_names_the_broken_field(
    target: str, field: str, value: Any, reason: str
) -> None:
    """Each wrong default is reported with the field that causes it.

    Mutates one field of the real chart and pins the reported reason, not just
    "something is wrong".
    """

    chart, values = _load(_CHART), _load(_VALUES)
    helpers = _HELPERS.read_text(encoding="utf-8")
    if target == "values":
        values["image"][field] = value
    else:
        chart[field] = value
    problems = default_problems(chart, values, helpers)
    assert any(reason in problem for problem in problems), problems


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('printf "%s-web" .Values.image.repository', 'printf "%s/web" .Values.image.repository'),
        ("$path := .Values.image.webRepository", "$path := .Values.image.repository"),
    ],
)
def test_mirror_refuses_a_changed_template(old: str, new: str) -> None:
    """If ``_helpers.tpl`` composes the image differently, the mirror says so."""

    chart, values = _load(_CHART), _load(_VALUES)
    helpers = _HELPERS.read_text(encoding="utf-8")
    assert old in helpers, f"test fixture out of date: {old!r} not in _helpers.tpl"
    with pytest.raises(AssertionError, match="composes the web image differently"):
        default_images(chart, values, helpers.replace(old, new))
