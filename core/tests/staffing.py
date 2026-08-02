# SPDX-License-Identifier: BUSL-1.1
"""Test helper: make a draft schema releasable (Stufe B, rule B2).

``operations.release`` enforces B2 -- every interactive step needs a staff rule
(BZR). Most fixtures in this suite are about something else entirely (control
flow, data flow, migration, composition) and only release a schema because they
need a runnable one. Without a helper each of them would have to grow an
organisation of its own, which would bury what the test actually asserts.

``staffed(schema)`` therefore does the minimum that makes a draft releasable:
it gives every *interactive* activity that has no rule yet a ROLE rule, adding a
role plus one bearer if the schema has no suitable organisation. Automatic steps
are skipped (Z4 forbids a BZR there), as are activities that already carry one --
so a fixture that staffs *some* steps deliberately keeps its own arrangement.

Like everything else in the codebase it goes through the public change
operations: no fixture builds a schema behind the validator's back.

Not a test module (no ``test_`` prefix), imported flat -- ``tests/`` is
deliberately not a package (see CLAUDE.md).
"""

from __future__ import annotations

from procworks import (
    NodeType,
    ProcessSchema,
    StaffRule,
    StaffRuleKind,
    add_agent,
    add_role,
    assign_staff_rule,
)

#: Ids of the role/agent the helper introduces. Stable and distinctive so a test
#: that inspects the organisation can tell them from its own master data.
TEST_ROLE_ID = "r-test"
TEST_AGENT_ID = "a-test"


def unstaffed_activities(schema: ProcessSchema) -> list[str]:
    """Ids of the interactive activities that still lack a staff rule (B2).

    Mirrors ``validator.check_executable``; kept here so the helper can decide
    whether it has anything to do without importing the validator's internals.
    """

    missing: list[str] = []
    for node in schema.nodes.values():
        if node.type is not NodeType.ACTIVITY:
            continue
        binding = schema.service_bindings.get(node.id)
        if binding is not None and binding.automatic:
            continue  # automatic step: no performer needed
        if node.id in schema.staff_rules:
            continue
        missing.append(node.id)
    return sorted(missing)


def _rule_for(schema: ProcessSchema) -> tuple[ProcessSchema, StaffRule]:
    """Return the schema (possibly extended) plus a satisfiable ROLE rule.

    A schema bound to a *shared* organisation cannot have its org edited through
    the schema (``operations._require_local_org``), so there the helper reuses an
    existing role that actually has a bearer -- otherwise Z2 would reject the
    rule as unsatisfiable.
    """

    org = schema.org_model
    if schema.org_model_id is not None:
        for role_id in sorted(org.roles):
            if any(role_id in agent.role_ids for agent in org.agents.values()):
                return schema, StaffRule(kind=StaffRuleKind.ROLE, ref=role_id)
        raise AssertionError(
            "cannot staff a schema on a shared org model without a borne role -- "
            "give the fixture's organisation a role with at least one agent"
        )

    if TEST_ROLE_ID not in org.roles:
        schema = add_role(schema, "Testrolle", role_id=TEST_ROLE_ID)
    if TEST_AGENT_ID not in schema.org_model.agents:
        schema = add_agent(
            schema,
            "Test Person",
            role_ids=[TEST_ROLE_ID],
            agent_id=TEST_AGENT_ID,
        )
    return schema, StaffRule(kind=StaffRuleKind.ROLE, ref=TEST_ROLE_ID)


def staffed(schema: ProcessSchema) -> ProcessSchema:
    """Give every unstaffed interactive step a BZR, so the schema can be released.

    Idempotent and a no-op for a schema that is already releasable, so it can be
    wrapped around any ``release(...)`` call without thinking about it.
    """

    targets = unstaffed_activities(schema)
    if not targets:
        return schema
    schema, rule = _rule_for(schema)
    for node_id in targets:
        schema = assign_staff_rule(schema, node_id, rule)
    return schema


def staff_via_api(
    client: object, schema_id: str, headers: dict[str, str] | None = None
) -> None:
    """The same, for a schema that a test builds over the HTTP API.

    API-level fixtures never hold a :class:`ProcessSchema` object, so they cannot
    use :func:`staffed`. This walks the same path a modeller would: add a role
    and a bearer, then assign a ROLE rule to every interactive step that has
    none. A no-op when nothing is missing.

    ``client`` is a ``TestClient``; ``headers`` carries the auth of tests that
    run with a token/password backend.
    """

    kwargs = {"headers": headers} if headers else {}
    schema = client.get(f"/schemas/{schema_id}", **kwargs).json()  # type: ignore[attr-defined]
    bindings = schema.get("service_bindings") or {}
    rules = schema.get("staff_rules") or {}
    targets = sorted(
        node_id
        for node_id, node in (schema.get("nodes") or {}).items()
        if node.get("type") == "ACTIVITY"
        and node_id not in rules
        and not (bindings.get(node_id) or {}).get("automatic")
    )
    if not targets:
        return

    org = schema.get("org_model") or {}
    if TEST_ROLE_ID not in (org.get("roles") or {}):
        client.post(  # type: ignore[attr-defined]
            f"/schemas/{schema_id}/roles",
            json={"name": "Testrolle", "role_id": TEST_ROLE_ID},
            **kwargs,
        )
    if TEST_AGENT_ID not in (org.get("agents") or {}):
        client.post(  # type: ignore[attr-defined]
            f"/schemas/{schema_id}/agents",
            json={
                "name": "Test Person",
                "role_ids": [TEST_ROLE_ID],
                "agent_id": TEST_AGENT_ID,
            },
            **kwargs,
        )
    for node_id in targets:
        client.post(  # type: ignore[attr-defined]
            f"/schemas/{schema_id}/staff-rule",
            json={"node_id": node_id, "rule": {"kind": "ROLE", "ref": TEST_ROLE_ID}},
            **kwargs,
        )
