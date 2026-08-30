# SPDX-License-Identifier: BUSL-1.1
"""Guards for the two shipped container images.

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
CI. The real verification of an image change stays manual (build + scan + run),
as recorded in CLAUDE.md.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEB_DOCKERFILE = _REPO_ROOT / "web" / "Dockerfile"
_API_DOCKERFILE = _REPO_ROOT / "core" / "Dockerfile"
_TRIVYIGNORE = _REPO_ROOT / ".trivyignore"
_RELEASE_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"


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
