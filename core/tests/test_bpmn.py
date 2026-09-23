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
    PriorityLevel,
    StaffRule,
    StaffRuleKind,
    TimeConstraint,
    WorkItemPriority,
    add_agent,
    add_data_element,
    add_org_unit,
    add_role,
    assign_staff_rule,
    conditional_insert,
    connect_data,
    create_empty_schema,
    export_bpmn,
    import_bpmn,
    insert_loop,
    parallel_insert,
    serial_insert,
    set_node_priority,
    set_org_unit_manager,
    set_time_constraint,
    validate,
)
from procworks.bpmn import BPMN_NS, BPMNDI_NS, DC_NS, DI_NS
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


def test_import_rejects_gateway_without_its_join() -> None:
    """A split with no join at all -- caught by K1's *counting* stage.

    Named for what it actually proves. It used to be called
    ``..._rejects_unstructured_graph_...``, which overstated it: the graph below
    also has unbalanced gateway *counts*, so it never exercised the nesting
    check at all. The genuine unstructured cases are the two tests below.
    """
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


def test_import_rejects_crossed_blocks_with_balanced_counts() -> None:
    """Two overlapping AND blocks -- the case gateway counting cannot see.

    This is the shape that reaches ProcWorks in practice: foreign BPMN is not
    block-structured (Section 2.1), so an import from another tool routinely
    looks like this. Before K1 checked nesting, this document imported with
    HTTP 201, could be released, and yielded instances that never completed.
    """

    xml = f"""<?xml version="1.0"?>
    <definitions xmlns="{BPMN_NS}">
      <process id="p">
        <startEvent id="start"/>
        <parallelGateway id="s1"/>
        <parallelGateway id="s2"/>
        <task id="p2"/><task id="q1"/><task id="q2"/>
        <parallelGateway id="j1"/>
        <parallelGateway id="j2"/>
        <endEvent id="end"/>
        <sequenceFlow id="f1" sourceRef="start" targetRef="s1"/>
        <sequenceFlow id="f2" sourceRef="s1" targetRef="s2"/>
        <sequenceFlow id="f3" sourceRef="s1" targetRef="p2"/>
        <sequenceFlow id="f4" sourceRef="s2" targetRef="q1"/>
        <sequenceFlow id="f5" sourceRef="s2" targetRef="q2"/>
        <sequenceFlow id="f6" sourceRef="q1" targetRef="j1"/>
        <sequenceFlow id="f7" sourceRef="p2" targetRef="j1"/>
        <sequenceFlow id="f8" sourceRef="q2" targetRef="j2"/>
        <sequenceFlow id="f9" sourceRef="j1" targetRef="j2"/>
        <sequenceFlow id="f10" sourceRef="j2" targetRef="end"/>
      </process>
    </definitions>"""
    with pytest.raises(CorrectnessError) as exc:
        import_bpmn(xml)
    assert any(
        f.rule == "K1" and "not properly nested" in f.message
        for f in exc.value.findings
    ), exc.value.findings


def test_import_rejects_split_closed_by_wrong_gateway_kind() -> None:
    """AND split closed by an exclusive join and vice versa; counts balance."""

    xml = f"""<?xml version="1.0"?>
    <definitions xmlns="{BPMN_NS}">
      <process id="p">
        <startEvent id="start"/>
        <parallelGateway id="as"/>
        <task id="a"/><task id="b"/>
        <exclusiveGateway id="xj"/>
        <exclusiveGateway id="xs"/>
        <task id="c"/><task id="d"/>
        <parallelGateway id="aj"/>
        <endEvent id="end"/>
        <sequenceFlow id="f1" sourceRef="start" targetRef="as"/>
        <sequenceFlow id="f2" sourceRef="as" targetRef="a"/>
        <sequenceFlow id="f3" sourceRef="as" targetRef="b"/>
        <sequenceFlow id="f4" sourceRef="a" targetRef="xj"/>
        <sequenceFlow id="f5" sourceRef="b" targetRef="xj"/>
        <sequenceFlow id="f6" sourceRef="xj" targetRef="xs"/>
        <sequenceFlow id="f7" sourceRef="xs" targetRef="c"/>
        <sequenceFlow id="f8" sourceRef="xs" targetRef="d"/>
        <sequenceFlow id="f9" sourceRef="c" targetRef="aj"/>
        <sequenceFlow id="f10" sourceRef="d" targetRef="aj"/>
        <sequenceFlow id="f11" sourceRef="aj" targetRef="end"/>
      </process>
    </definitions>"""
    with pytest.raises(CorrectnessError) as exc:
        import_bpmn(xml)
    assert any(
        f.rule == "K1" and "is closed by" in f.message for f in exc.value.findings
    ), exc.value.findings


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


# --- lanes, diagram interchange and the staffing round-trip ---------------
#
# Der Nachtest 2026-09-22 (Mangel 4) fand drei Luecken auf einmal: der Export
# trug keine Diagramm-Koordinaten (Fremdwerkzeuge zeigten eine leere Flaeche),
# keine Lanes (nicht ablesbar, wer was tut) und der Rundlauf verlor die
# Bearbeiterregeln ("3 Schritt(e) ohne Bearbeiter").


def _staffed() -> object:
    """Sequenz mit Vier-Augen-Regel: erfassen (Rolle) -> freigeben (Vorgesetzte:r)."""

    schema = create_empty_schema("Vier Augen", schema_id="va")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erfassen = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
    schema = serial_insert(schema, "Freigeben", after_node_id=erfassen)
    freigeben = next(n.id for n in schema.nodes.values() if n.label == "Freigeben")
    schema = add_role(schema, "Sachbearbeiter", role_id="sb")
    schema = add_org_unit(schema, "Vertrieb", org_unit_id="vertrieb")
    schema = add_agent(
        schema, "Erika Sander", role_ids=["sb"], org_unit_id="vertrieb", agent_id="a-erika"
    )
    schema = add_agent(
        schema, "Tom Berger", role_ids=["sb"], org_unit_id="vertrieb", agent_id="a-tom"
    )
    schema = set_org_unit_manager(schema, "vertrieb", "a-tom")
    schema = assign_staff_rule(schema, erfassen, StaffRule(kind=StaffRuleKind.ROLE, ref="sb"))
    schema = assign_staff_rule(
        schema,
        freigeben,
        StaffRule(kind=StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR, ref=erfassen),
    )
    return schema


def test_round_trip_keeps_the_staff_rules_and_their_organisation() -> None:
    """Der Rundlauf verlor bisher jede Bearbeiterzuordnung.

    Die Regeln allein reichen nicht: ohne die Organisation, gegen die sie
    aufloesen, wiese der validierende Import sie unter Z1/Z2 zurueck. Beides
    reist deshalb in der ProcWorks-Erweiterung mit.
    """

    original = _staffed()
    restored = import_bpmn(export_bpmn(original))

    assert validate(restored) == []
    assert restored.staff_rules == original.staff_rules
    assert set(restored.org_model.agents) == {"a-erika", "a-tom"}
    assert restored.org_model.org_units["vertrieb"].manager_id == "a-tom"
    # Eine geteilte Organisation reist als eigene Stammdaten mit, nicht als Link:
    # die importierende Installation kennt deren Registry nicht.
    assert restored.org_model_id is None


def test_export_writes_one_lane_per_staff_rule_with_a_readable_name() -> None:
    process = ET.fromstring(export_bpmn(_staffed())).find(_q("process"))
    assert process is not None
    lane_set = process.find(_q("laneSet"))
    assert lane_set is not None
    lanes = lane_set.findall(_q("lane"))

    assert [lane.get("name") for lane in lanes] == [
        "Sachbearbeiter",
        "Vorgesetzte:r von „Erfassen“",
    ]
    # Jede Lane nennt ihre Schritte -- und nur die zugeordneten: Ereignisse und
    # Gateways behauptet das Modell niemandem.
    refs = [r.text for lane in lanes for r in lane.findall(_q("flowNodeRef"))]
    assert len(refs) == 2
    assert "start" not in refs


def test_export_without_any_staff_rule_writes_no_lanes() -> None:
    process = ET.fromstring(export_bpmn(_sequential())).find(_q("process"))
    assert process is not None
    assert process.find(_q("laneSet")) is None


def test_export_carries_diagram_interchange_for_every_node_and_flow() -> None:
    """Ohne BPMNDI oeffnen Fremdwerkzeuge eine leere Flaeche."""

    schema = _conditional()
    root = ET.fromstring(export_bpmn(schema))
    plane = root.find(f"{{{BPMNDI_NS}}}BPMNDiagram/{{{BPMNDI_NS}}}BPMNPlane")
    assert plane is not None
    assert plane.get("bpmnElement") == schema.id

    shapes = {s.get("bpmnElement") for s in plane.findall(f"{{{BPMNDI_NS}}}BPMNShape")}
    assert set(schema.nodes) <= shapes
    process = root.find(_q("process"))
    assert process is not None
    flow_ids = {f.get("id") for f in process.findall(_q("sequenceFlow"))}
    edge_ids = {e.get("bpmnElement") for e in plane.findall(f"{{{BPMNDI_NS}}}BPMNEdge")}
    assert flow_ids == edge_ids
    # Jede Form hat eine Groesse, jede Kante mindestens zwei Stuetzpunkte.
    for shape in plane.findall(f"{{{BPMNDI_NS}}}BPMNShape"):
        bounds = shape.find(f"{{{DC_NS}}}Bounds")
        assert bounds is not None and float(bounds.get("width") or 0) > 0
    for edge in plane.findall(f"{{{BPMNDI_NS}}}BPMNEdge"):
        assert len(edge.findall(f"{{{DI_NS}}}waypoint")) >= 2


def test_diagram_places_a_step_inside_its_lane_band() -> None:
    """Die Lane ist nur dann eine Aussage, wenn ihr Schritt auch darin liegt."""

    schema = _staffed()
    root = ET.fromstring(export_bpmn(schema))
    plane = root.find(f"{{{BPMNDI_NS}}}BPMNDiagram/{{{BPMNDI_NS}}}BPMNPlane")
    assert plane is not None
    boxes = {}
    for shape in plane.findall(f"{{{BPMNDI_NS}}}BPMNShape"):
        b = shape.find(f"{{{DC_NS}}}Bounds")
        assert b is not None
        boxes[shape.get("bpmnElement")] = (float(b.get("y") or 0), float(b.get("height") or 0))

    freigeben = next(n.id for n in schema.nodes.values() if n.label == "Freigeben")
    lane_y, lane_h = boxes["lane_2"]
    node_y, node_h = boxes[freigeben]
    assert lane_y <= node_y and node_y + node_h <= lane_y + lane_h
    # ... und die beiden Lanes ueberlappen einander nicht.
    first_y, first_h = boxes["lane_1"]
    assert first_y + first_h <= lane_y


def test_diagram_routes_a_loop_back_flow_below_the_nodes() -> None:
    """Die abgeleitete Rücksprungkante zeigt rueckwaerts -- gerade gezeichnet
    liefe sie quer durch den Ablauf."""

    schema = create_empty_schema("Schleife", schema_id="loop")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erfassen = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
    schema = add_data_element(schema, "nacharbeit", DataType.BOOLEAN, element_id="na")
    schema = insert_loop(schema, erfassen, "Pruefen", discriminator="na")

    root = ET.fromstring(export_bpmn(schema))
    plane = root.find(f"{{{BPMNDI_NS}}}BPMNDiagram/{{{BPMNDI_NS}}}BPMNPlane")
    assert plane is not None
    back = next(
        e for e in plane.findall(f"{{{BPMNDI_NS}}}BPMNEdge")
        if (e.get("bpmnElement") or "").startswith("loopflow")
    )
    points = [
        (float(w.get("x") or 0), float(w.get("y") or 0))
        for w in back.findall(f"{{{DI_NS}}}waypoint")
    ]

    assert points[0][0] > points[-1][0]  # laeuft nach links zurueck
    others = [
        float(s.find(f"{{{DC_NS}}}Bounds").get("y") or 0)  # type: ignore[union-attr]
        + float(s.find(f"{{{DC_NS}}}Bounds").get("height") or 0)  # type: ignore[union-attr]
        for s in plane.findall(f"{{{BPMNDI_NS}}}BPMNShape")
    ]
    assert max(p[1] for p in points) > max(others)  # unter allen Knoten entlang


def test_import_ignores_lanes_and_diagram_of_a_foreign_document() -> None:
    """Beides ist Darstellung. Ein Fremddokument bringt sie mit, das Modell
    entsteht trotzdem nur aus den semantischen Elementen."""

    xml = f"""<?xml version="1.0"?>
    <definitions xmlns="{BPMN_NS}" xmlns:bpmndi="{BPMNDI_NS}" xmlns:dc="{DC_NS}">
      <process id="p" name="Fremd">
        <laneSet id="ls">
          <lane id="l1" name="Sachbearbeitung"><flowNodeRef>u</flowNodeRef></lane>
        </laneSet>
        <startEvent id="start"/>
        <userTask id="u" name="Pruefen"/>
        <endEvent id="end"/>
        <sequenceFlow id="f1" sourceRef="start" targetRef="u"/>
        <sequenceFlow id="f2" sourceRef="u" targetRef="end"/>
      </process>
      <bpmndi:BPMNDiagram id="d">
        <bpmndi:BPMNPlane id="pl" bpmnElement="p">
          <bpmndi:BPMNShape id="s1" bpmnElement="u">
            <dc:Bounds x="10" y="20" width="100" height="80"/>
          </bpmndi:BPMNShape>
        </bpmndi:BPMNPlane>
      </bpmndi:BPMNDiagram>
    </definitions>"""

    schema = import_bpmn(xml)

    assert validate(schema) == []
    assert schema.staff_rules == {}  # eine Lane ist keine Bearbeiterregel
    assert sum(1 for n in schema.nodes.values() if n.type is NodeType.ACTIVITY) == 1


def test_round_trip_keeps_the_annotation_layers() -> None:
    """Masken, Zeiten, Prioritaeten und Eskalationen gingen ebenfalls verloren."""

    schema = _staffed()
    erfassen = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
    schema = set_time_constraint(schema, erfassen, TimeConstraint(target_seconds=3600))
    schema = set_node_priority(schema, erfassen, WorkItemPriority(level=PriorityLevel.HIGH))

    restored = import_bpmn(export_bpmn(schema))

    assert restored.time_constraints == schema.time_constraints
    assert restored.node_priorities == schema.node_priorities
