# SPDX-License-Identifier: BUSL-1.1
"""Every finding of the core carries a ``code`` (Validierung 2026-09-25, VAL-07).

The client words findings from one catalogue (``FINDING_TEXTS``) and falls back
to the English ``message`` only when a finding has no code. The validation saw
that fallback in the UI -- ``ACTIVITY must have in=1, out=1 (in=1, out=2)``,
``XOR split has no branch decision``, ``unknown role …``, ``loop discriminator
… (K6c)``, ``an agent cannot be its own deputy``, ``node … is already reached``.

Two guards: statically, no ``ValidationFinding(...)`` in ``src/`` is built
without ``code=`` (the ``fail`` helpers of the rule checks take ``code`` as a
required keyword, which mypy enforces); at runtime, the six examples above
come back with a code. ``test_catalog_covers_every_code_the_core_emits`` makes
sure each code has a German text.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from staffing import staffed

from procworks import (
    ExecutionContext,
    add_role,
    adhoc_insert_activity,
    assign_staff_rule,
    create_empty_schema,
    create_org_model,
    instantiate,
    org_add_agent,
    org_set_deputy,
    release,
    serial_insert,
    validate,
)
from procworks.model import (
    ControlEdge,
    Node,
    NodeType,
    StaffRule,
    StaffRuleKind,
)
from procworks.store import InMemoryInstanceStore
from procworks.validator import CorrectnessError

SRC = Path(__file__).resolve().parents[1] / "src" / "procworks"


def test_no_validation_finding_is_built_without_a_code() -> None:
    missing: list[str] = []
    for py in sorted(SRC.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            if name != "ValidationFinding":
                continue
            keywords = {k.arg for k in node.keywords}
            if None in keywords:  # ValidationFinding(**data): a rebuilt finding
                continue
            if "code" not in keywords:
                missing.append(f"{py.name}:{node.lineno}")
    assert not missing, "ValidationFinding ohne code=: " + ", ".join(missing)


def _codes(exc: pytest.ExceptionInfo[CorrectnessError]) -> list[tuple[str, str | None]]:
    return [(f.rule, f.code) for f in exc.value.findings]


def test_degree_finding_has_a_code() -> None:
    schema = serial_insert(create_empty_schema("K2", schema_id="k2c"), "A", after_node_id="start")
    act = next(n.id for n in schema.nodes.values() if n.label == "A")
    schema.nodes["x"] = Node(id="x", type=NodeType.ACTIVITY, label="X")
    schema.edges.append(ControlEdge(source=act, target="x"))
    schema.edges.append(ControlEdge(source="x", target="end"))

    codes = {(f.rule, f.code) for f in validate(schema)}

    assert ("K2", "K2.degree") in codes
    assert all(code for _, code in codes)


def test_unknown_role_in_a_staff_rule_has_a_code() -> None:
    schema = serial_insert(create_empty_schema("Z1", schema_id="z1c"), "A", after_node_id="start")
    act = next(n.id for n in schema.nodes.values() if n.label == "A")

    with pytest.raises(CorrectnessError) as exc:
        assign_staff_rule(schema, act, StaffRule(kind=StaffRuleKind.ROLE, ref="ghost"))

    assert ("Z1", "Z1.unknown-role") in _codes(exc)
    finding = next(f for f in exc.value.findings if f.code == "Z1.unknown-role")
    assert finding.params == {"ref": "ghost"}


def test_own_deputy_in_a_shared_org_has_a_code() -> None:
    org = create_org_model("Org", org_id="org-deputy-code")
    org = org_add_agent(org, "Erika", agent_id="a1")

    with pytest.raises(CorrectnessError) as exc:
        org_set_deputy(org, "a1", "a1")

    assert _codes(exc) == [("OP", "OP.own-deputy")]


def test_adhoc_on_a_reached_node_has_a_code() -> None:
    schema = serial_insert(create_empty_schema("R1", schema_id="r1c"), "A", after_node_id="start")
    schema = add_role(schema, "SB", role_id="sb")
    schema = release(staffed(schema))
    context = ExecutionContext(lambda *_: None, InMemoryInstanceStore())
    instance = instantiate(schema, context=context)

    with pytest.raises(CorrectnessError) as exc:
        adhoc_insert_activity(instance, schema, "start", "Zu spät")

    [(rule, code)] = _codes(exc)
    assert rule == "R1" and code is not None and code.startswith("R1.")


def test_xor_without_decision_and_loop_discriminator_findings_have_codes() -> None:
    # Built as raw graphs (like a BPMN import would), then validated.
    xor = create_empty_schema("K7", schema_id="k7c")
    xor.nodes = {
        "start": Node(id="start", type=NodeType.START),
        "s": Node(id="s", type=NodeType.XOR_SPLIT),
        "a": Node(id="a", type=NodeType.ACTIVITY, label="A"),
        "b": Node(id="b", type=NodeType.ACTIVITY, label="B"),
        "j": Node(id="j", type=NodeType.XOR_JOIN),
        "end": Node(id="end", type=NodeType.END),
    }
    xor.edges = [
        ControlEdge(source="start", target="s"),
        ControlEdge(source="s", target="a"),
        ControlEdge(source="s", target="b"),
        ControlEdge(source="a", target="j"),
        ControlEdge(source="b", target="j"),
        ControlEdge(source="j", target="end"),
    ]
    assert ("K7", "K7.no-decision") in {(f.rule, f.code) for f in validate(xor)}

    loop = create_empty_schema("K6", schema_id="k6c")
    loop.nodes = {
        "start": Node(id="start", type=NodeType.START),
        "ls": Node(id="ls", type=NodeType.LOOP_START),
        "a": Node(id="a", type=NodeType.ACTIVITY, label="A"),
        "le": Node(id="le", type=NodeType.LOOP_END),
        "end": Node(id="end", type=NodeType.END),
    }
    loop.edges = [
        ControlEdge(source="start", target="ls"),
        ControlEdge(source="ls", target="a"),
        ControlEdge(source="a", target="le"),
        ControlEdge(source="le", target="end"),
    ]
    findings = validate(loop)
    assert findings and all(f.code for f in findings)
    assert ("K6", "K6.no-decision") in {(f.rule, f.code) for f in findings}
