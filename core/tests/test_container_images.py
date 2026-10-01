# SPDX-License-Identifier: BUSL-1.1
"""Guards for the two shipped container images (and the internal demo image).

The release workflow gates on a Trivy scan: if it finds a fixable HIGH/CRITICAL
issue, the image is **not** pushed and the version tag ends up without an
artifact (that cost the v1.9.0/v1.9.1 releases). Two properties keep that gate
green, and both are easy to undo by accident:

1. **The web image compiles Caddy itself.** The official ``caddy:2-alpine``
   ships a pre-built binary whose Go toolchain keeps ageing between Caddy
   releases; on 2026-08-30 it had accumulated 14 HIGH findings that we could not
   fix, forcing 13 scan exceptions. Rebuilding the *same* Caddy version with the
   builder image's newer toolchain removed all of them. Reverting to a plain
   ``FROM caddy:2-alpine`` would silently bring the backlog back -- the image
   would still work, so nothing but the next release would notice.
2. **Both build stages must reference the same Caddy tag.** The version that
   gets compiled comes from ``CADDY_VERSION`` inside the *builder* image. Bump
   only one of the two tags and the image runs a different Caddy than its
   runtime layer was built for.

Plus the rule for the exception list itself: an entry is a last resort and has
to say when it may go away, otherwise nobody ever removes it.

These are text checks against the repository -- there is no Docker daemon in
CI. The real verification of an image change stays manual (build + scan + run).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEB_DOCKERFILE = _REPO_ROOT / "web" / "Dockerfile"
_API_DOCKERFILE = _REPO_ROOT / "core" / "Dockerfile"
_TRIVYIGNORE = _REPO_ROOT / ".trivyignore"
_RELEASE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"

#: The release pipeline (workflow + scan exceptions) lives only in the internal
#: repository; the public release repo carries the Dockerfiles but not the CI.
#: Guards about the pipeline skip there instead of failing a customer's run.
_needs_release_pipeline = pytest.mark.skipif(
    not (_TRIVYIGNORE.exists() and _RELEASE_WORKFLOW.exists()),
    reason="release pipeline (.trivyignore, release.yml) is not part of this checkout",
)


def test_web_image_compiles_caddy_from_source() -> None:
    """The self-build must not silently degrade back to the prebuilt binary."""

    text = _WEB_DOCKERFILE.read_text(encoding="utf-8")
    assert "caddy:2-builder-alpine" in text, (
        "web/Dockerfile no longer uses the Caddy builder image -- the prebuilt "
        "binary carries whatever Go version the last Caddy release shipped with"
    )
    assert re.search(r"go build\b", text), "no 'go build' step left in web/Dockerfile"
    assert re.search(r"COPY --from=builder\s+/usr/bin/caddy\s+/usr/bin/caddy", text), (
        "the compiled binary is not copied over the one from the base image"
    )


def test_both_caddy_stages_use_the_same_tag() -> None:
    """Builder and runtime stage must stay on the same Caddy tag."""

    text = _WEB_DOCKERFILE.read_text(encoding="utf-8")
    # ``FROM`` kann ein ``--platform=...`` tragen (Kreuz-Uebersetzung), deshalb
    # optional mitparsen statt strikt am Zeilenanfang zu verankern.
    stage = r"^FROM (?:--platform=\S+ )?caddy:"
    builder = re.findall(stage + r"(\S+)-builder-alpine", text, re.M)
    runtime = [
        tag
        for tag in re.findall(stage + r"(\S+?)-alpine", text, re.M)
        if not tag.endswith("-builder")
    ]
    assert len(builder) == 1, f"expected exactly one builder stage, got {builder}"
    assert len(runtime) == 1, f"expected exactly one runtime stage, got {runtime}"
    assert builder[0] == runtime[0], (
        f"Caddy tags drifted apart: builder is '{builder[0]}', runtime is "
        f"'{runtime[0]}'. The compiled version comes from the builder image, so "
        f"the runtime layer would host a different Caddy than it expects."
    )


def test_api_image_applies_base_image_security_updates() -> None:
    """``python:3.12-slim`` lags behind Debian's security updates between rebuilds."""

    text = _API_DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"apt-get\s+upgrade", text), (
        "core/Dockerfile no longer refreshes the base image packages -- on "
        "2026-08-30 that gap was worth 40 HIGH findings"
    )


@_needs_release_pipeline
def test_every_trivy_exception_states_when_it_may_be_removed() -> None:
    """An exception without a resolution condition never gets cleaned up.

    Empty is the expected state and passes vacuously. Each entry must be
    preceded by a comment block naming the condition under which it goes away.
    """

    lines = _TRIVYIGNORE.read_text(encoding="utf-8").splitlines()
    undocumented = []
    for index, line in enumerate(lines):
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        block = []
        cursor = index - 1
        while cursor >= 0 and lines[cursor].lstrip().startswith("#"):
            block.append(lines[cursor])
            cursor -= 1
        if "entfernen, sobald" not in " ".join(block).lower():
            undocumented.append(entry)
    assert not undocumented, (
        "Trivy exceptions without a resolution condition "
        "(add a comment saying 'entfernen, sobald ...'): " + ", ".join(undocumented)
    )


@_needs_release_pipeline
def test_release_builds_and_scans_both_architectures() -> None:
    """Images must ship for amd64 *and* arm64, and every architecture is scanned.

    Until 2026-08-30 the release built amd64 only, so ``docker pull`` on an ARM
    machine (Apple Silicon, ARM servers) failed with "no matching manifest".
    Dropping a platform again would be invisible until someone on the wrong
    architecture tried to install -- and scanning only one of them would let a
    finding hide behind the other.
    """

    text = _RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "platforms: linux/amd64,linux/arm64" in text, (
        "the pushed image is no longer multi-arch"
    )
    for platform in ("linux/amd64", "linux/arm64"):
        assert f"platforms: {platform}\n" in text, f"no separate build for {platform}"
    assert text.count("aquasecurity/trivy-action") >= 2, (
        "each architecture needs its own Trivy scan -- one scan cannot cover both"
    )


@_needs_release_pipeline
def test_release_can_be_rehearsed_without_a_version_tag() -> None:
    """A Dockerfile change must be provable without burning a version tag.

    A tag cannot be withdrawn, and a failing scan leaves it without an artifact
    (v1.9.0/v1.9.1). The manual trigger builds and scans without publishing, so
    the push step has to be conditional.
    """

    text = _RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text, "release.yml cannot be triggered manually"
    assert "if: github.event_name != 'workflow_dispatch' || inputs.push" in text, (
        "the push step is not guarded -- a rehearsal run would publish images"
    )


#: OCI labels the published images must carry, as written in release.yml.
#: The images are public while this repository is private: without these
#: overrides ``docker/metadata-action`` derives source/url from
#: ``github.repository`` (a 404 for every customer), puts an internal commit into
#: ``revision`` and reports the licence as ``NOASSERTION``. An empty value is a
#: deliberate override -- it displaces the default instead of dropping the key.
_EXPECTED_OCI_METADATA = {
    "org.opencontainers.image.source": "https://github.com/tobiasHaecker/procworks-release",
    "org.opencontainers.image.url": "https://github.com/tobiasHaecker/procworks-release",
    "org.opencontainers.image.licenses": "BUSL-1.1",
    "org.opencontainers.image.title": "procworks-${{ matrix.name }}",
    "org.opencontainers.image.revision": "",
}


def _metadata_block(step: str, key: str) -> dict[str, str] | None:
    """Parse a ``key: |`` block of ``key=value`` lines from a workflow step.

    Args:
        step: The text of one workflow step (as found by
            :func:`_oci_metadata_problems`).
        key: The ``with:`` input holding the block, e.g. ``"labels"``.

    Returns:
        The entries as a mapping (value may be empty), or ``None`` when the
        step has no such block. Lines without ``=`` are ignored; a repeated key
        keeps its last value, like metadata-action does.
    """

    match = re.search(rf"^([ \t]*){key}: \|\n((?:\1[ \t]+\S.*\n?)+)", step, re.M)
    if match is None:
        return None
    entries: dict[str, str] = {}
    for line in match.group(2).splitlines():
        name, sep, value = line.strip().partition("=")
        if sep:
            entries[name] = value.strip()
    return entries


def _oci_metadata_problems(workflow: str) -> list[str]:
    """List every way ``release.yml`` would publish internal metadata.

    Checks, on the raw workflow text (no Docker, no YAML dependency):

    * no ``type=sha`` tag (it names an internal commit);
    * the ``docker/metadata-action`` step sets every entry of
      :data:`_EXPECTED_OCI_METADATA` plus a non-empty ``description``, both in
      ``labels`` (image config) and in ``annotations`` (manifests);
    * annotations also reach the multi-arch index and are handed to the push.

    Args:
        workflow: Full text of a release workflow.

    Returns:
        Human-readable problems, each naming the offending label, block or tag;
        empty when the workflow is correct.
    """

    problems: list[str] = []
    if re.search(r"^\s*type=sha\b", workflow, re.M):
        problems.append("tags still contain 'type=sha' (sha-<internal commit> tag)")
    step_match = re.search(
        r"uses: docker/metadata-action@\S+\n(.*?)(?=^\s*- name:|\Z)", workflow, re.M | re.S
    )
    if step_match is None:
        return [*problems, "no docker/metadata-action step found"]
    step = step_match.group(1)
    for block in ("labels", "annotations"):
        entries = _metadata_block(step, block)
        if entries is None:
            problems.append(f"metadata-action sets no '{block}' block")
            continue
        for name, expected in _EXPECTED_OCI_METADATA.items():
            if name not in entries:
                problems.append(f"{block}: '{name}' missing")
            elif entries[name] != expected:
                problems.append(
                    f"{block}: '{name}' is '{entries[name]}', expected '{expected}'"
                )
        if not entries.get("org.opencontainers.image.description"):
            problems.append(f"{block}: 'org.opencontainers.image.description' missing or empty")
    if not re.search(r"DOCKER_METADATA_ANNOTATIONS_LEVELS:\s*\S*\bindex\b", step):
        problems.append("annotations do not reach the multi-arch index level")
    if "annotations: ${{ steps.meta.outputs.annotations }}" not in workflow:
        problems.append("the push step does not pass 'annotations' from metadata-action")
    return problems


@_needs_release_pipeline
def test_release_images_point_to_the_public_repo_without_internal_commits() -> None:
    """Public images must not link to the private repo or name internal commits.

    The 2026-09-30 audit found every published tag labelled with the private
    repository as source/url, an internal commit as revision, ``NOASSERTION``
    as licence and extra ``sha-<commit>`` tags -- all metadata-action defaults.
    """

    problems = _oci_metadata_problems(_RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert not problems, "release.yml leaks internal metadata:\n" + "\n".join(problems)


@_needs_release_pipeline
@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        (
            "type=semver,pattern={{major}}.{{minor}}\n",
            "type=semver,pattern={{major}}.{{minor}}\n            type=sha\n",
            "type=sha",
        ),
        (
            "            org.opencontainers.image.licenses=BUSL-1.1\n",
            "",
            "labels: 'org.opencontainers.image.licenses' missing",
        ),
        (
            "org.opencontainers.image.revision=\n",
            "org.opencontainers.image.revision=${{ github.sha }}\n",
            "labels: 'org.opencontainers.image.revision' is '${{ github.sha }}'",
        ),
        (
            "org.opencontainers.image.source=https://github.com/tobiasHaecker/procworks-release",
            "org.opencontainers.image.source=https://example.invalid/private",
            "labels: 'org.opencontainers.image.source' is 'https://example.invalid/private'",
        ),
        (
            "org.opencontainers.image.title=procworks-${{ matrix.name }}",
            "org.opencontainers.image.title=procworks",
            "labels: 'org.opencontainers.image.title' is 'procworks'",
        ),
        (
            "org.opencontainers.image.description=${{ matrix.description }}",
            "org.opencontainers.image.description=",
            "labels: 'org.opencontainers.image.description' missing or empty",
        ),
        (
            "          annotations: |\n",
            "          notes: |\n",
            "metadata-action sets no 'annotations' block",
        ),
        (
            "          annotations: ${{ steps.meta.outputs.annotations }}\n",
            "",
            "the push step does not pass 'annotations'",
        ),
        (
            "DOCKER_METADATA_ANNOTATIONS_LEVELS: manifest,index",
            "DOCKER_METADATA_ANNOTATIONS_LEVELS: manifest",
            "multi-arch index",
        ),
    ],
)
def test_oci_metadata_check_names_the_broken_entry(old: str, new: str, reason: str) -> None:
    """Each defect is reported with the label, block or tag that causes it.

    Mutates the real workflow once (first occurrence only, so for labels the
    ``labels`` block is hit and ``annotations`` stays intact) and pins the
    reported reason, not just "something is wrong".
    """

    workflow = _RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert old in workflow, f"test fixture out of date: {old!r} not in release.yml"
    problems = _oci_metadata_problems(workflow.replace(old, new, 1))
    assert any(reason in problem for problem in problems), problems


# --- The web image's own build recipe must not be served ---------------------

_ROOT_DOCKERIGNORE = _REPO_ROOT / ".dockerignore"
_DEMO_DOCKERFILE = _REPO_ROOT / "deploy" / "demo" / "Dockerfile"

#: Root ``.dockerignore`` and the demo Dockerfile exist only in the internal
#: repository (neither is synced); a customer checkout skips these guards.
_needs_internal_build_files = pytest.mark.skipif(
    not (_ROOT_DOCKERIGNORE.exists() and _DEMO_DOCKERFILE.exists()),
    reason="root .dockerignore and deploy/demo/Dockerfile exist only internally",
)

#: Per image: (label, COPY of web/, removal of the copied Dockerfile).
_WEB_COPY_AND_REMOVE = {
    "web": (
        "web/Dockerfile",
        re.compile(r"^COPY\s+web/?\s+/srv/?\s*$", re.M),
        re.compile(r"^RUN\s+rm\s+-f\s+/srv/Dockerfile\s*$", re.M),
    ),
    "demo": (
        "deploy/demo/Dockerfile",
        re.compile(r"^COPY\s+web/?\s+\./web/?\s*$", re.M),
        re.compile(r"^RUN\s+rm\s+-f\s+\./web/Dockerfile\s*$", re.M),
    ),
}


def _copy_then_remove_problems(image: str, dockerfile: str) -> list[str]:
    """Check that an image deletes ``web/Dockerfile`` right after copying ``web/``.

    ``web/Dockerfile`` copies ``web/`` wholesale to ``/srv/`` and the demo image
    copies it to ``/app/web`` (``PROCWORKS_WEB_DIR``); both serve that
    directory, so the build recipe was reachable as ``/Dockerfile``. The
    ``.dockerignore`` exclusion only helps where that file exists -- the
    release repo has none (neither for a customer's ``docker compose --build``
    nor for the demo, built from a release-repo clone) -- so each Dockerfile
    removes the copied file itself.

    Args:
        image: ``"web"`` or ``"demo"`` (key of :data:`_WEB_COPY_AND_REMOVE`).
        dockerfile: Text of that image's Dockerfile.

    Returns:
        Human-readable problems naming the file and the missing step; empty
        when the file is copied and then removed.
    """

    label, copy_re, remove_re = _WEB_COPY_AND_REMOVE[image]
    copy = copy_re.search(dockerfile)
    remove = remove_re.search(dockerfile)
    if copy is None:
        return [f"{label}: no COPY of web/ found"]
    if remove is None:
        return [f"{label}: the copied Dockerfile is not removed"]
    if remove.start() < copy.start():
        return [f"{label}: the Dockerfile is removed before web/ is copied"]
    return []


def _dockerignore_problems(dockerignore: str) -> list[str]:
    """Check that the root ``.dockerignore`` keeps ``web/Dockerfile`` out.

    Defence in depth for the published web image (built from the internal
    repository root) -- the file then never reaches a layer at all. BuildKit
    still reads the Dockerfile given with ``-f``; only COPY/ADD no longer see
    it.

    Args:
        dockerignore: Text of the root ``.dockerignore``.

    Returns:
        Human-readable problems naming the pattern; empty when it is excluded
        and not re-included by a later ``!web/Dockerfile``.
    """

    patterns = [
        line.strip()
        for line in dockerignore.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if "web/Dockerfile" not in patterns:
        return [".dockerignore: 'web/Dockerfile' is not excluded"]
    if "!web/Dockerfile" in patterns[patterns.index("web/Dockerfile") :]:
        return [".dockerignore: 'web/Dockerfile' is re-included by '!web/Dockerfile'"]
    return []


def test_web_image_does_not_serve_its_dockerfile() -> None:
    """The shipped web/Dockerfile removes itself from ``/srv`` -- in any checkout."""

    problems = _copy_then_remove_problems("web", _WEB_DOCKERFILE.read_text(encoding="utf-8"))
    assert not problems, problems


@_needs_internal_build_files
def test_demo_image_and_build_context_keep_the_dockerfile_out() -> None:
    """Demo image removes it; the root ``.dockerignore`` keeps it out of the context."""

    problems = [
        *_copy_then_remove_problems("demo", _DEMO_DOCKERFILE.read_text(encoding="utf-8")),
        *_dockerignore_problems(_ROOT_DOCKERIGNORE.read_text(encoding="utf-8")),
    ]
    assert not problems, "web/Dockerfile would be served:\n" + "\n".join(problems)


@pytest.mark.parametrize(
    ("image", "old", "new", "reason"),
    [
        (
            "web",
            "RUN rm -f /srv/Dockerfile\n",
            "",
            "web/Dockerfile: the copied Dockerfile is not removed",
        ),
        (
            "web",
            "COPY web/ /srv/\nRUN rm -f /srv/Dockerfile\n",
            "RUN rm -f /srv/Dockerfile\nCOPY web/ /srv/\n",
            "web/Dockerfile: the Dockerfile is removed before web/ is copied",
        ),
        (
            "web",
            "COPY web/ /srv/\n",
            "COPY web/app.js /srv/\n",
            "web/Dockerfile: no COPY of web/ found",
        ),
        (
            "demo",
            "RUN rm -f ./web/Dockerfile\n",
            "",
            "deploy/demo/Dockerfile: the copied Dockerfile is not removed",
        ),
        (
            "demo",
            "COPY web ./web\nRUN rm -f ./web/Dockerfile\n",
            "RUN rm -f ./web/Dockerfile\nCOPY web ./web\n",
            "deploy/demo/Dockerfile: the Dockerfile is removed before web/ is copied",
        ),
    ],
)
def test_copy_then_remove_check_names_the_missing_step(
    image: str, old: str, new: str, reason: str
) -> None:
    """Each broken removal is reported with the file and the reason.

    Mutates the real Dockerfile once and pins the reported reason.
    """

    path = _WEB_DOCKERFILE if image == "web" else _DEMO_DOCKERFILE
    if not path.exists():
        pytest.skip(f"{path.name} is not part of this checkout")
    text = path.read_text(encoding="utf-8")
    assert old in text, f"test fixture out of date: {old!r} not in {path}"
    assert _copy_then_remove_problems(image, text.replace(old, new, 1)) == [reason]


@_needs_internal_build_files
@pytest.mark.parametrize(
    ("new", "reason"),
    [
        ("\n", "'web/Dockerfile' is not excluded"),
        ("\n# web/Dockerfile\n", "'web/Dockerfile' is not excluded"),
        ("\nweb/Dockerfile\n!web/Dockerfile\n", "re-included by '!web/Dockerfile'"),
    ],
)
def test_dockerignore_check_names_the_missing_pattern(new: str, reason: str) -> None:
    """A missing or re-included exclusion is reported with the pattern."""

    text = _ROOT_DOCKERIGNORE.read_text(encoding="utf-8")
    old = "\nweb/Dockerfile\n"
    assert old in text, "test fixture out of date: 'web/Dockerfile' not in .dockerignore"
    problems = _dockerignore_problems(text.replace(old, new, 1))
    assert len(problems) == 1 and reason in problems[0], problems
