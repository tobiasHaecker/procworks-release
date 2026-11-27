# SPDX-License-Identifier: BUSL-1.1
"""A test instance of a draft starts only when its old findings are harmless at run time.

Since the repair path a draft may carry old findings (a rule got stricter
after it was stored). Starting a throw-away test instance of such a draft was
always allowed. For findings the runtime protects itself against (a time
value beyond 100 years, an unsafe input pattern) that is fine; for every
other finding (staffing, structure, data flow, a system table) no runtime
guard exists, and the test instance could hang or fail. Now such a draft
refuses the test instance with ``422`` ``LC.test-incorrect`` -- one finding
per offending rule finding, ``params.rule`` and ``params.finding`` naming it --
exactly the tolerance a migration applies (``RUNTIME_SAFE_OLD_FINDINGS``).
"""

from __future__ import annotations

import itertools

from fastapi.testclient import TestClient
from staffing import staffed

import procworks.api as api_module
from procworks import (
    StaffRule,
    StaffRuleKind,
    TimeConstraint,
    create_empty_schema,
    serial_insert,
    validate,
)
from procworks.migration import draft_test_findings
from procworks.model import ProcessSchema

client = TestClient(api_module.app)
_ids = (f"draft-test-{n}" for n in itertools.count(1))


def _draft() -> tuple[ProcessSchema, str]:
    schema = create_empty_schema("Prüfinstanz", schema_id=next(_ids))
    schema = serial_insert(schema, "A", after_node_id="start")
    step = next(n.id for n in schema.nodes.values() if n.label == "A")
    return staffed(schema), step


def _with_unknown_role(schema: ProcessSchema, step: str) -> ProcessSchema:
    """A staffing finding no runtime guard covers (planted behind the operations)."""

    aged = schema.model_copy(deep=True)
    aged.staff_rules[step] = StaffRule(kind=StaffRuleKind.ROLE, ref="gibt-es-nicht")
    assert any(f.rule == "Z1" for f in validate(aged))
    return aged


def _with_huge_time(schema: ProcessSchema, step: str) -> ProcessSchema:
    aged = schema.model_copy(deep=True)
    aged.time_constraints[step] = TimeConstraint(max_duration_seconds=1e12)
    assert [f.code for f in validate(aged)] == ["T1.duration-too-large"]
    return aged


def test_a_clean_draft_has_nothing_to_report() -> None:
    schema, _ = _draft()
    assert draft_test_findings(schema) == []


def test_a_runtime_safe_old_finding_is_tolerated() -> None:
    schema, step = _draft()
    assert draft_test_findings(_with_huge_time(schema, step)) == []


def test_another_old_finding_is_named() -> None:
    schema, step = _draft()
    [finding] = draft_test_findings(_with_unknown_role(schema, step))
    assert (finding.rule, finding.code) == ("LC", "LC.test-incorrect")
    assert finding.params["rule"] == "Z1"
    assert finding.params["finding"].startswith("Z1.")
    assert finding.node_id == step


def test_api_refuses_the_test_instance_and_creates_none() -> None:
    schema, step = _draft()
    aged = _with_unknown_role(schema, step)
    api_module._store.put(aged)
    before = len(api_module._instances.list_ids())

    resp = client.post(f"/schemas/{aged.id}/instances")

    assert resp.status_code == 422, resp.text
    codes = {f["code"] for f in resp.json()["detail"]["findings"]}
    assert codes == {"LC.test-incorrect"}
    assert len(api_module._instances.list_ids()) == before


def test_api_starts_the_test_instance_with_a_runtime_safe_old_finding() -> None:
    schema, step = _draft()
    aged = _with_huge_time(schema, step)
    api_module._store.put(aged)

    resp = client.post(f"/schemas/{aged.id}/instances")

    assert resp.status_code == 201, resp.text
    assert resp.json()["is_test"] is True
