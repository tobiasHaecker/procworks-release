# SPDX-License-Identifier: BUSL-1.1
"""A shared organisation with a finding it already had stays maintainable.

Every change of a shared organisation is checked before it is stored. It used
to count *every* finding of the result: an organisation stored with a finding
(a rule that got stricter after it was saved, e.g. a mail address the check
now refuses, or data written before a check existed) could not be changed at
all -- not even by the change that removes the finding. Now a change is
refused only for the findings it **adds** (the comparison every model edit
uses), and the refusal names exactly those.
"""

from __future__ import annotations

import pytest

from procworks import (
    CorrectnessError,
    create_org_model,
    org_add_agent,
    org_add_role,
    org_add_unit,
    org_set_deputy,
    org_update_agent,
    validate_org,
)
from procworks.model import OrgModel


def _aged() -> OrgModel:
    """An organisation as an update finds it: one agent with a mail address
    the address check refuses (written behind the operations)."""

    org = create_org_model("Amt", org_id="org-aged")
    org = org_add_role(org, "SB", role_id="sb")
    org = org_add_agent(org, "Erika", role_ids=["sb"], agent_id="a1")
    org = org_add_agent(org, "Tom", agent_id="a2")
    aged = org.model_copy(deep=True)
    aged.agents["a1"] = aged.agents["a1"].model_copy(update={"email": "erika@@kaputt"})
    assert [f.rule for f in validate_org(aged)] == ["N1"]
    return aged


def test_an_unrelated_change_is_accepted_next_to_an_old_finding() -> None:
    org = org_add_unit(_aged(), "Vertrieb", org_unit_id="u1")
    org = org_set_deputy(org, "a2", "a1")
    assert "u1" in org.org_units
    assert [f.rule for f in validate_org(org)] == ["N1"]


def test_the_old_finding_can_be_repaired() -> None:
    org = org_update_agent(_aged(), "a1", email="erika@example.org")
    assert validate_org(org) == []


def test_a_change_that_adds_a_finding_is_refused_for_that_one_only() -> None:
    with pytest.raises(CorrectnessError) as exc:
        org_update_agent(_aged(), "a2", email="tom@@kaputt")

    [finding] = exc.value.findings
    assert finding.rule == "N1"
    assert "a2" in finding.message
    assert "a1" not in finding.message


def test_a_clean_organisation_is_still_checked_completely() -> None:
    """Negativ: without old findings every finding of a change counts as before."""

    org = create_org_model("Sauber", org_id="org-clean")
    org = org_add_agent(org, "Erika", agent_id="a1")
    with pytest.raises(CorrectnessError) as exc:
        org_update_agent(org, "a1", email="kein-at")
    assert [f.rule for f in exc.value.findings] == ["N1"]


def test_api_maintains_a_stored_organisation_with_an_old_finding() -> None:
    from fastapi.testclient import TestClient

    import procworks.api as api_module

    client = TestClient(api_module.app)
    org_id = "org-aged-api"
    created = client.post("/org-models", json={"name": "Amt", "org_model_id": org_id})
    assert created.status_code == 201
    client.post(f"/org-models/{org_id}/agents", json={"name": "Erika", "agent_id": "x1"})
    stored = api_module._org_store.get(org_id)
    assert stored is not None
    aged = stored.model_copy(deep=True)
    aged.agents["x1"] = aged.agents["x1"].model_copy(update={"email": "erika@@kaputt"})
    api_module._org_store.put(aged)

    added = client.post(f"/org-models/{org_id}/roles", json={"name": "SB", "role_id": "sb"})
    assert added.status_code < 300, added.text

    worse = client.post(
        f"/org-models/{org_id}/agents",
        json={"name": "Tom", "agent_id": "x2", "email": "tom@@kaputt"},
    )
    assert worse.status_code == 422
    assert [f["rule"] for f in worse.json()["detail"]["findings"]] == ["N1"]
    assert "x2" in worse.json()["detail"]["findings"][0]["message"]
