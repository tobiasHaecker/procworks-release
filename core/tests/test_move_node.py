# SPDX-License-Identifier: BUSL-1.1
"""move_node: relocate a step without losing its bindings (§7.2 moveNode).

The operation composes the serial splice-out of ``delete_node`` with the
splice-in of ``serial_insert`` into one atomic, validated transformation. These
tests pin the contract: bindings travel with the node, the XOR empty-branch
semantics mirror deletion, and every move that would break a correctness rule
(D1 forward-reference first among them) is rejected with the schema unchanged.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from staffing import staffed

from procworks import (
    AccessMode,
    BranchSpec,
    DataType,
    add_data_element,
    conditional_insert,
    connect_data,
    create_empty_schema,
    move_node,
    parallel_insert,
    release,
    serial_insert,
    validate,
)
from procworks.api import app
from procworks.model import NodeType
from procworks.operations import CorrectnessError

client = TestClient(app)


def _nid(schema: object, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)  # type: ignore[attr-defined]


def _split_id(schema: object, node_type: NodeType = NodeType.XOR_SPLIT) -> str:
    return next(n.id for n in schema.nodes.values() if n.type is node_type)  # type: ignore[attr-defined]


def _sequence(schema: object) -> list[str]:
    """The node ids along the outgoing chain from start (serial schemas only)."""

    order = ["start"]
    while True:
        out = schema.outgoing(order[-1])  # type: ignore[attr-defined]
        if not out:
            return order
        order.append(out[0].target)


def _abc_schema():
    """start -> A -> B -> C -> end, plus a data element written by A, read by C."""

    schema = create_empty_schema("Sequenz")
    schema = serial_insert(schema, "A", after_node_id="start")
    schema = serial_insert(schema, "B", after_node_id=_nid(schema, "A"))
    schema = serial_insert(schema, "C", after_node_id=_nid(schema, "B"))
    schema = add_data_element(schema, "x", DataType.INTEGER, element_id="x")
    schema = connect_data(schema, _nid(schema, "A"), "x", AccessMode.WRITE)
    schema = connect_data(schema, _nid(schema, "C"), "x", AccessMode.READ)
    return schema


def _threshold_schema():
    """start -> Erfassen(write betrag) -> XOR(Team | Leitung) -> end."""

    schema = create_empty_schema("Schwelle")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erfassen = _nid(schema, "Erfassen")
    schema = add_data_element(schema, "betrag", DataType.INTEGER, element_id="betrag")
    schema = connect_data(schema, erfassen, "betrag", AccessMode.WRITE)
    return conditional_insert(
        schema,
        after_node_id=erfassen,
        discriminator="betrag",
        branches=[BranchSpec(label="Team", upper=1001), BranchSpec(label="Leitung")],
    )


# --- positive cases -------------------------------------------------------


def test_move_within_a_sequence_relocates_the_node() -> None:
    schema = _abc_schema()
    a, b, c = _nid(schema, "A"), _nid(schema, "B"), _nid(schema, "C")

    moved = move_node(schema, b, after_node_id=c)

    assert _sequence(moved) == ["start", a, c, b, moved.end_node().id]
    assert validate(moved) == []


def test_move_backwards_towards_start() -> None:
    schema = _abc_schema()
    b = _nid(schema, "B")

    moved = move_node(schema, b, after_node_id="start")

    assert _sequence(moved)[1] == b
    assert validate(moved) == []


def test_move_keeps_all_bindings_of_the_node() -> None:
    schema = staffed(_abc_schema())
    b = _nid(schema, "B")
    schema = connect_data(schema, b, "x", AccessMode.READ)

    moved = move_node(schema, b, after_node_id=_nid(schema, "C"))

    assert any(
        a.node_id == b and a.element_id == "x" for a in moved.data_accesses
    ), "data access must travel with the moved node"
    assert b in moved.staff_rules, "staff rule must travel with the moved node"
    assert validate(moved) == []


def test_move_after_current_predecessor_is_a_no_op() -> None:
    schema = _abc_schema()
    b = _nid(schema, "B")

    moved = move_node(schema, b, after_node_id=_nid(schema, "A"))

    assert _sequence(moved) == _sequence(schema)
    assert validate(moved) == []


def test_move_sole_xor_branch_node_leaves_an_empty_branch() -> None:
    schema = _threshold_schema()
    team = _nid(schema, "Team")
    split = _split_id(schema)
    join = _split_id(schema, NodeType.XOR_JOIN)

    moved = move_node(schema, team, after_node_id=_nid(schema, "Erfassen"))

    # The branch survives as a direct split -> join edge keeping its K7 cell.
    assert any(e.source == split and e.target == join for e in moved.edges)
    decision = moved.xor_decisions[split]
    assert any(b.target == join and b.upper == 1001 for b in decision.branches)
    # The node itself now sits between Erfassen and the split.
    assert moved.outgoing(team)[0].target == split
    assert validate(moved) == []


def test_move_first_node_of_a_longer_xor_branch_retargets_the_cell() -> None:
    schema = _threshold_schema()
    team = _nid(schema, "Team")
    schema = serial_insert(schema, "Team2", after_node_id=team)
    team2 = _nid(schema, "Team2")
    split = _split_id(schema)

    moved = move_node(schema, team, after_node_id=team2)

    decision = moved.xor_decisions[split]
    assert any(b.target == team2 for b in decision.branches)
    caption_edge = next(e for e in moved.outgoing(split) if e.target == team2)
    assert caption_edge.condition, "derived caption must be refreshed on the new head"
    assert validate(moved) == []


def test_move_via_api_endpoint() -> None:
    resp = client.post("/schemas", json={"name": "Move-API"})
    sid = resp.json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Eins", "after_node_id": "start"}
    )
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    eins = next(nid for nid, n in nodes.items() if n.get("label") == "Eins")
    client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Zwei", "after_node_id": eins}
    )
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    zwei = next(nid for nid, n in nodes.items() if n.get("label") == "Zwei")

    resp = client.post(
        f"/schemas/{sid}/nodes/{eins}/move", json={"after_node_id": zwei}
    )
    assert resp.status_code == 200
    schema = resp.json()
    assert any(
        e["source"] == zwei and e["target"] == eins for e in schema["edges"]
    )


# --- rejected moves (validate-before-commit) ------------------------------


def test_move_writer_after_its_reader_is_rejected_d1() -> None:
    schema = _abc_schema()

    with pytest.raises(CorrectnessError) as err:
        move_node(schema, _nid(schema, "A"), after_node_id=_nid(schema, "C"))
    assert any(f.rule == "D1" for f in err.value.findings)


def test_move_reader_before_its_writer_is_rejected_d1() -> None:
    schema = _abc_schema()

    with pytest.raises(CorrectnessError) as err:
        move_node(schema, _nid(schema, "C"), after_node_id="start")
    assert any(f.rule == "D1" for f in err.value.findings)


def test_move_sole_and_branch_node_is_rejected() -> None:
    schema = create_empty_schema("Parallel")
    schema = parallel_insert(schema, ["Links", "Rechts"], after_node_id="start")

    with pytest.raises(CorrectnessError) as err:
        move_node(schema, _nid(schema, "Links"), after_node_id="start")
    assert "parallel branch" in err.value.findings[0].message


def test_move_second_xor_branch_empty_is_rejected() -> None:
    schema = _threshold_schema()
    erfassen = _nid(schema, "Erfassen")
    schema = move_node(schema, _nid(schema, "Team"), after_node_id=erfassen)

    with pytest.raises(CorrectnessError) as err:
        move_node(schema, _nid(schema, "Leitung"), after_node_id=erfassen)
    assert "non-empty branch" in err.value.findings[0].message


def test_move_rejects_start_end_gateways_and_self() -> None:
    schema = _threshold_schema()
    team = _nid(schema, "Team")
    split = _split_id(schema)

    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        move_node(schema, "start", after_node_id=team)
    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        move_node(schema, split, after_node_id=team)
    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        move_node(schema, team, after_node_id=team)
    with pytest.raises(CorrectnessError, match=r"\[OP\]"):
        move_node(schema, team, after_node_id=schema.end_node().id)


def test_move_rejects_a_split_as_anchor() -> None:
    schema = _threshold_schema()

    with pytest.raises(CorrectnessError) as err:
        move_node(schema, _nid(schema, "Erfassen"), after_node_id=_split_id(schema))
    assert "outgoing" in err.value.findings[0].message


def test_move_rejects_released_schema() -> None:
    schema = _abc_schema()
    schema = release(staffed(schema))

    with pytest.raises(CorrectnessError, match=r"\[R0\]"):
        move_node(schema, _nid(schema, "B"), after_node_id=_nid(schema, "C"))


def test_rejected_move_leaves_the_schema_unchanged() -> None:
    schema = _abc_schema()
    before = schema.model_dump()

    with pytest.raises(CorrectnessError, match=r"\[D1\]"):
        move_node(schema, _nid(schema, "A"), after_node_id=_nid(schema, "C"))
    assert schema.model_dump() == before


def test_move_invalid_via_api_returns_422_with_findings() -> None:
    resp = client.post("/schemas", json={"name": "Move-422"})
    sid = resp.json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Solo", "after_node_id": "start"}
    )
    nodes = client.get(f"/schemas/{sid}").json()["nodes"]
    solo = next(nid for nid, n in nodes.items() if n.get("label") == "Solo")

    resp = client.post(
        f"/schemas/{sid}/nodes/{solo}/move", json={"after_node_id": solo}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["findings"]
