# SPDX-License-Identifier: BUSL-1.1
"""The API pods are replaced all at once on an update (``Recreate``).

With the Kubernetes default ``RollingUpdate``, pods of the old and the new
version run side by side for a while on the same database. An older process
knows nothing of what the update introduced -- the conditional save, the
lock of the audit chain end, the shared reservations -- so in that window it
silently overwrites concurrent changes and appends to the audit chain
without the lock (the check then reports a defect). The chart therefore
stops every old API pod before the new ones start; the short downtime is
the price. An operator who wants rolling updates sets ``api.strategy``
deliberately.

There is no ``helm`` in the test environment: the default is read from
``values.yaml`` and the template is checked to take it from there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_HELM_DIR = Path(__file__).resolve().parents[2] / "deploy" / "helm"

pytestmark = pytest.mark.skipif(not _HELM_DIR.is_dir(), reason="Helm chart not present")


def test_the_api_is_updated_with_recreate_by_default() -> None:
    values = yaml.safe_load((_HELM_DIR / "values.yaml").read_text(encoding="utf-8"))

    assert values["api"]["strategy"] == {"type": "Recreate"}


def test_the_api_deployment_takes_its_strategy_from_the_values() -> None:
    template = (_HELM_DIR / "templates" / "api-deployment.yaml").read_text(encoding="utf-8")

    assert "strategy:" in template
    assert "toYaml .Values.api.strategy" in template
    # the strategy belongs to the deployment spec, before the pod template
    assert template.index("strategy:") < template.index("template:")
