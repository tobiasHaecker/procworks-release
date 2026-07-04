# SPDX-License-Identifier: BUSL-1.1
"""BPMN 2.0 import/export tests (roadmap step 14, Section 2.3).

Export maps the block-structured schema onto semantic BPMN; import maps it back
and -- in line with the no-bypass principle -- validates the result, so an
unstructured BPMN graph can never become a stored, incorrect model.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from procworks import (
    AccessMode,
    BpmnError,
    BranchSpec,
    DataType,
    NodeType,
    add_data_element,
    conditional_insert,
    connect_data,
    create_empty_schema,
    export_bpmn,
    import_bpmn,
    parallel_insert,
    serial_insert,
    validate,
)
from procworks.bpmn import BPMN_NS
from procworks.validator import CorrectnessError


def _q(local: str) -> str:
    return f"{{{BPMN_NS}}}{local}"


def _sequential() -> object:
    schema = create_empty_schema("Sequence", schema_id="seq")
    return serial_insert(schema, "Erfassen", after_node_id="start")


def _parallel() -> object:
    schema = create_empty_schema("Parallel", schema_id="par")
    return parallel_insert(schema, ["Fachpruefung", "Budgetpruefung"], after_node_id="start")


def _conditional() -> object:
    schema = create_empty_schema("Bedingt", schema_id="xor")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erfassen = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
    schema = add_data_element(schema, "betrag", DataType.INTEGER, element_id="betrag")
    schema = connect_data(schema, erfassen, "betrag", AccessMode.WRITE)
    return conditional_insert(
        schema,
        after_node_id=erfassen,
        discriminator="betrag",
        branches=[
            BranchSpec(label="Freigabe Team", upper=1001),
            BranchSpec(label="Freigabe Leitung"),
        ],
    )


# --- export --------------------------------------------------------------


def test_export_produces_valid_bpmn_xml() -> None:
    xml = export_bpmn(_sequential())
    root = ET.fromstring(xml)
    assert root.tag == _q("definitions")
    process = root.find(_q("process"))
    assert process is not None
    assert process.find(_q("startEvent")) is not None
    assert process.find(_q("endEvent")) is not None
    assert process.find(_q("task")) is not None


def test_export_maps_gateways() -> None:
    xml = export_bpmn(_parallel())
    process = ET.fromstring(xml).find(_q("process"))
    assert process is not None
    assert len(process.findall(_q("parallelGateway"))) == 2


def test_export_writes_xor_conditions() -> None:
    xml = export_bpmn(_conditional())
    process = ET.fromstring(xml).find(_q("process"))
    assert process is not None
    conditions = {
        (flow.find(_q("conditionExpression")).text or "")
        for flow in process.findall(_q("sequenceFlow"))
        if flow.find(_q("conditionExpression")) is not None
    }
    assert conditions == {"betrag < 1001", "betrag >= 1001"}


# --- round-trip ----------------------------------------------------------


def _node_type_multiset(schema: object) -> dict[NodeType, int]:
    counts: dict[NodeType, int] = {}
    for node in schema.nodes.values():  # type: ignore[attr-defined]
        counts[node.type] = counts.get(node.type, 0) + 1
    return counts


@pytest.mark.parametrize("builder", [_sequential, _parallel, _conditional])
def test_round_trip_preserves_structure(builder: object) -> None:
    original = builder()  # type: ignore[operator]
    restored = import_bpmn(export_bpmn(original))
    assert validate(restored) == []
    assert _node_type_multiset(restored) == _node_type_multiset(original)
    assert len(restored.edges) == len(original.edges)


def test_round_trip_preserves_xor_conditions() -> None:
    original = _conditional()
    restored = import_bpmn(export_bpmn(original))
    assert {e.condition for e in restored.edges if e.condition} == {
        "betrag < 1001",
        "betrag >= 1001",
    }
    # the structured decision survives the round-trip (K7 holds on import)
    assert len(restored.xor_decisions) == 1


def test_round_trip_infers_split_and_join_from_degree() -> None:
    restored = import_bpmn(export_bpmn(_parallel()))
    types = _node_type_multiset(restored)
    assert types.get(NodeType.AND_SPLIT) == 1
    assert types.get(NodeType.AND_JOIN) == 1


# --- import rejection (no bypass) ----------------------------------------


def test_import_rejects_malformed_xml() -> None:
    with pytest.raises(BpmnError):
        import_bpmn("<definitions>broken")


def test_import_rejects_unsupported_element() -> None:
    xml = f"""<?xml version="1.0"?>
    <definitions xmlns="{BPMN_NS}">
      <process id="p">
        <startEvent id="start"/>
        <inclusiveGateway id="g"/>
        <endEvent id="end"/>
        <sequenceFlow id="f1" sourceRef="start" targetRef="g"/>
        <sequenceFlow id="f2" sourceRef="g" targetRef="end"/>
      </process>
    </definitions>"""
    with pytest.raises(BpmnError):
        import_bpmn(xml)


def test_import_rejects_unstructured_graph_via_validator() -> None:
    # A parallel split closed by an exclusive join is not block-structured (K1).
    xml = f"""<?xml version="1.0"?>
    <definitions xmlns="{BPMN_NS}">
      <process id="p">
        <startEvent id="start"/>
        <parallelGateway id="split"/>
        <task id="a"/>
        <task id="b"/>
        <exclusiveGateway id="join"/>
        <endEvent id="end"/>
        <sequenceFlow id="f1" sourceRef="start" targetRef="split"/>
        <sequenceFlow id="f2" sourceRef="split" targetRef="a"/>
        <sequenceFlow id="f3" sourceRef="split" targetRef="b"/>
        <sequenceFlow id="f4" sourceRef="a" targetRef="join"/>
        <sequenceFlow id="f5" sourceRef="b" targetRef="join"/>
        <sequenceFlow id="f6" sourceRef="join" targetRef="end"/>
      </process>
    </definitions>"""
    with pytest.raises(CorrectnessError) as exc:
        import_bpmn(xml)
    assert any(f.rule == "K1" for f in exc.value.findings)


def test_import_rejects_mixed_gateway() -> None:
    # A gateway with two in and two out is neither a pure split nor join.
    xml = f"""<?xml version="1.0"?>
    <definitions xmlns="{BPMN_NS}">
      <process id="p">
        <startEvent id="start"/>
        <task id="a"/>
        <task id="b"/>
        <parallelGateway id="g"/>
        <task id="c"/>
        <task id="d"/>
        <endEvent id="end"/>
        <sequenceFlow id="f1" sourceRef="start" targetRef="a"/>
        <sequenceFlow id="f2" sourceRef="a" targetRef="g"/>
        <sequenceFlow id="f3" sourceRef="b" targetRef="g"/>
        <sequenceFlow id="f4" sourceRef="g" targetRef="c"/>
        <sequenceFlow id="f5" sourceRef="g" targetRef="d"/>
        <sequenceFlow id="f6" sourceRef="c" targetRef="end"/>
        <sequenceFlow id="f7" sourceRef="d" targetRef="b"/>
      </process>
    </definitions>"""
    with pytest.raises(BpmnError):
        import_bpmn(xml)


def test_import_accepts_task_subtypes() -> None:
    xml = f"""<?xml version="1.0"?>
    <definitions xmlns="{BPMN_NS}">
      <process id="p" name="Sub">
        <startEvent id="start"/>
        <userTask id="u" name="Pruefen"/>
        <endEvent id="end"/>
        <sequenceFlow id="f1" sourceRef="start" targetRef="u"/>
        <sequenceFlow id="f2" sourceRef="u" targetRef="end"/>
      </process>
    </definitions>"""
    schema = import_bpmn(xml)
    assert validate(schema) == []
    assert sum(1 for n in schema.nodes.values() if n.type is NodeType.ACTIVITY) == 1


def test_import_uses_overridden_id_and_name() -> None:
    schema = import_bpmn(export_bpmn(_sequential()), schema_id="custom", name="Neu")
    assert schema.id == "custom"
    assert schema.name == "Neu"
