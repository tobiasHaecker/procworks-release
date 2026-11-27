# SPDX-License-Identifier: BUSL-1.1
"""Aufgaben aus Test-Instanzen stehen in keiner persönlichen Arbeitsliste.

Ein Modellierer spielt einen Entwurf als Test-Instanz durch (Prüfinstanz,
``GET /instances/{id}/tasks``). In „Meine Aufgaben“ sahen sie aus wie echte
Aufgaben -- auch bei der Vertretung -- und ließen sich unbemerkt übernehmen
und abschließen. Mails und Eskalationen schweigen für Test-Instanzen schon
länger; die Arbeitslisten jetzt auch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

import procworks.api as api_module
from procworks import (
    add_agent,
    add_role,
    assign_staff_rule,
    create_empty_schema,
    instantiate,
    release,
    serial_insert,
    set_agent_deputy,
)
from procworks.api import app
from procworks.model import StaffRule, StaffRuleKind


def _schemas(schema_id: str):  # type: ignore[no-untyped-def]
    """Draft and released revision of one step for Erika (a-ti1), deputy a-ti2."""
    schema = serial_insert(create_empty_schema("TI", schema_id=schema_id), "Bearbeiten",
                           after_node_id="start")
    act = next(n.id for n in schema.nodes.values() if n.label == "Bearbeiten")
    schema = add_role(schema, "SB", role_id="sb-ti")
    schema = add_agent(schema, "Erika", role_ids=["sb-ti"], agent_id="a-ti1")
    schema = add_agent(schema, "Vertretung", agent_id="a-ti2")
    schema = assign_staff_rule(schema, act, StaffRule(kind=StaffRuleKind.ROLE, ref="sb-ti"))
    schema = set_agent_deputy(schema, "a-ti1", "a-ti2")
    return schema, release(schema), act


def test_test_instance_stays_out_of_personal_and_deputy_worklists() -> None:
    draft, released, act = _schemas("ti-1")
    real = instantiate(released)
    test = instantiate(draft, allow_unreleased=True, is_test=True)
    api_module._store.put(released)
    api_module._instances.put(real)
    api_module._instances.put(test)
    now = datetime.now(UTC)

    with TestClient(app) as client:
        own = {t["instance_id"] for t in client.get("/agents/a-ti1/tasks").json()}
        assert real.id in own
        assert test.id not in own  # Grund: Test-Instanz, nicht fehlende Zustaendigkeit

        # Die Instanz selbst kennt die Aufgabe weiterhin (Pruefinstanz-Cockpit).
        inst_tasks = client.get(f"/instances/{test.id}/tasks").json()
        assert [t["node_id"] for t in inst_tasks] == [act]
        assert "a-ti1" in inst_tasks[0]["eligible_agents"]

        created = client.post("/agents/a-ti1/absences", json={
            "start_at": (now - timedelta(hours=1)).isoformat(),
            "end_at": (now + timedelta(hours=1)).isoformat(),
        })
        assert created.status_code == 201
        try:
            deputy = {t["instance_id"] for t in client.get("/agents/a-ti2/tasks").json()}
            assert real.id in deputy
            assert test.id not in deputy
        finally:
            client.delete(f"/agents/a-ti1/absences/{created.json()['id']}")
