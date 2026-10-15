# SPDX-License-Identifier: BUSL-1.1
"""The retry budget of an automatic step is bounded (``retry_max`` <= 100).

``retry_backoff_ms`` and ``request_timeout_ms`` got upper bounds; the number
of retries did not: ``retry_max`` 10**400 was accepted, and such a step never
became an incident -- it retried for ever, or until its back-off could no
longer be computed. Now:

* the validator refuses a budget above :data:`procworks.model.MAX_RETRY_MAX`
  with ``I1.retries-too-large`` (``params.max``), the bound itself is fine;
* a budget stored before the bound is capped at run time
  (:func:`procworks.integration_runtime.retry_budget`), never an error;
* because the runtime protects itself, the old finding is runtime-safe: it
  does not block a migration onto such a target, and a draft carrying it can
  still be edited -- and repaired.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_retry_backoff_timeout import _automated, _runtime, _with

from procworks import CorrectnessError, instantiate, release, set_automation
from procworks.api import app
from procworks.execution import ExecutionContext
from procworks.integration_runtime import retry_budget
from procworks.migration import RUNTIME_SAFE_OLD_FINDINGS, check_migration
from procworks.model import MAX_RETRY_MAX, AutomationKind, ServiceBinding
from procworks.store import InMemoryInstanceStore, InMemorySchemaStore, make_resolver
from procworks.validator import validate

client = TestClient(app)


def test_the_bound_is_a_hundred_and_accepted_itself() -> None:
    assert MAX_RETRY_MAX == 100
    [binding] = _automated(retry_max=MAX_RETRY_MAX).service_bindings.values()
    assert binding.retry_max == MAX_RETRY_MAX


@pytest.mark.parametrize("value", [MAX_RETRY_MAX + 1, 10**400], ids=["101", "riesig"])
def test_a_budget_above_the_bound_is_refused_for_that_reason(value: int) -> None:
    with pytest.raises(CorrectnessError) as refused:
        _automated(retry_max=value)

    [finding] = refused.value.findings
    assert (finding.rule, finding.code) == ("I1", "I1.retries-too-large")
    assert finding.params["max"] == str(MAX_RETRY_MAX)


def test_the_api_answers_the_code() -> None:
    sid = client.post("/schemas", json={"name": "Wiederholungen"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "Lauf", "after_node_id": "start"})
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    node_id = next(nid for nid, n in nodes.items() if n["type"] == "ACTIVITY")
    client.post(f"/schemas/{sid}/service",
                json={"node_id": node_id, "name": "Lauf", "automatic": True})

    resp = client.post(f"/schemas/{sid}/automation", json={
        "node_id": node_id, "automation": "EXTERNAL_TASK", "topic": "x", "retry_max": 1000,
    })

    assert resp.status_code == 422, resp.text
    assert [f["code"] for f in resp.json()["detail"]["findings"]] == ["I1.retries-too-large"]


@pytest.mark.parametrize("stored", [10**6, 10**400], ids=["million", "riesig"])
def test_a_stored_huge_budget_is_capped_at_run_time(stored: int) -> None:
    runtime, clock = _runtime(retry_max=3, backoff_ms=1_000, age=_with(retry_max=stored))

    [task] = runtime.fetch_and_lock("w1", ["robust"], lock_ms=60_000)
    assert task.retries_left == MAX_RETRY_MAX

    failed = runtime.failure(task.id, "w1", "upstream down")

    assert failed.retries_left == MAX_RETRY_MAX - 1
    assert failed.available_at == pytest.approx(clock.t + 1.0)  # still the first failure


@pytest.mark.parametrize(("stored", "budget"), [(-3, 0), (0, 0), (7, 7), (10**400, 100)])
def test_retry_budget_bounds_the_modelled_value(stored: int, budget: int) -> None:
    assert retry_budget(ServiceBinding(node_id="n", name="x", retry_max=stored)) == budget


def test_the_old_finding_is_runtime_safe_and_does_not_block_a_migration() -> None:
    assert "I1.retries-too-large" in RUNTIME_SAFE_OLD_FINDINGS
    source = release(_automated(retry_max=3))
    target = _with(retry_max=10**400)(source).model_copy(
        update={"id": "budget-v2", "version": 2, "revision_of": source.id}
    )
    schemas = InMemorySchemaStore()
    schemas.put(source)
    case = instantiate(source, context=ExecutionContext(make_resolver(schemas),
                                                         InMemoryInstanceStore()))

    assert check_migration(case, source, target) == []


def test_a_draft_with_the_old_value_can_be_repaired() -> None:
    draft = _with(retry_max=10**400)(_automated(retry_max=3))
    assert "I1.retries-too-large" in {f.code for f in validate(draft)}
    [node_id] = draft.service_bindings

    repaired = set_automation(draft, node_id, AutomationKind.EXTERNAL_TASK, topic="robust",
                              retry_max=5)

    assert "I1.retries-too-large" not in {f.code for f in validate(repaired)}
