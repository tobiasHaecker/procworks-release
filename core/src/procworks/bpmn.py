# SPDX-License-Identifier: BUSL-1.1
"""BPMN 2.0 import/export (Section 2.3, roadmap step 14).

The internal block-structured meta-model is the single source of truth for
correctness and execution; BPMN 2.0 is supported as an interchange format on
the *checked, executable subset* (Sequence, AND, XOR, Sub-Process).

Export maps a schema to semantic BPMN XML. Import does the opposite, but -- in
line with the no-bypass principle (Section 1.1.3) -- the mapped graph is run
through the correctness validator before it is returned: an unstructured BPMN
graph (e.g. arbitrary or inclusive-OR gateways) is rejected, never stored as an
incorrect model.

The mapping covers the semantic model plus, since the Nachtest 2026-09-22, the
*drawn* diagram: an export carries lanes (one per staff rule), diagram
interchange (BPMNDI shapes, edges and lane bands) and the ProcWorks extension
that round-trips everything BPMN cannot say -- staff rules, the organisation
they resolve against, forms, times, priorities, escalations. Import ignores
lanes and diagram entirely: both are presentation, the model comes from the
semantic elements and the extension.

    START          <-> bpmn:startEvent
    END            <-> bpmn:endEvent
    ACTIVITY       <-> bpmn:task (userTask/serviceTask/... import as ACTIVITY)
    AND_SPLIT/JOIN <-> bpmn:parallelGateway   (role inferred from degree)
    XOR_SPLIT/JOIN <-> bpmn:exclusiveGateway  (role inferred from degree)
    SUBPROCESS     ->  bpmn:callActivity       (export only; calledElement)
    ControlEdge    <-> bpmn:sequenceFlow (+ conditionExpression on XOR branches)
    LOOP_START/END <-> bpmn:exclusiveGateway pair + loop-back sequenceFlow

Loops (K6, stage S3): a loop exports as the canonical REPEAT-UNTIL gateway
pattern -- an exclusiveGateway pair whose back flow ``LOOP_END -> LOOP_START``
carries the derived repeat predicate. Internally the back edge is never a
datum (the stored graph stays acyclic); it exists only in the interchange
document. The import recognises the pattern structurally *before* node-type
resolution: a flow between two exclusive gateways of the right degrees whose
*target still reaches its source without that flow* is a loop-back edge (that
reachability test is what tells a loop apart from an empty XOR branch, where
the join can never reach the split). The back flow is dropped, the pair is
retyped to LOOP_START/LOOP_END, and the structured ``LoopDecision`` comes from
the ProcWorks extension -- a recognised cycle without one fails K6b in the
validating import (No-Bypass), it is never stored undecidable.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from pydantic import TypeAdapter

from procworks.model import (
    ActivityTemplate,
    ConnectorDescriptor,
    ControlEdge,
    DataAccess,
    DataElement,
    EdgeType,
    EscalationPolicy,
    Form,
    LoopDecision,
    MailBinding,
    Node,
    NodeType,
    OrgModel,
    ProcessSchema,
    ServiceBinding,
    StaffRule,
    TimeConstraint,
    WorkItemPriority,
    XorDecision,
    loop_block,
    loop_condition_text,
    staff_rule_text,
)
from procworks.validator import SchemaResolver, raise_if_invalid

_DATA_ELEMENTS = TypeAdapter(list[DataElement])
_DATA_ACCESSES = TypeAdapter(list[DataAccess])
_XOR_DECISIONS = TypeAdapter(dict[str, XorDecision])
_LOOP_DECISIONS = TypeAdapter(dict[str, LoopDecision])
_FORMS = TypeAdapter(dict[str, Form])
_CONNECTORS = TypeAdapter(dict[str, ConnectorDescriptor])
_STAFF_RULES = TypeAdapter(dict[str, StaffRule])
_ORG_MODEL = TypeAdapter(OrgModel)
_SERVICE_BINDINGS = TypeAdapter(dict[str, ServiceBinding])
_ACTIVITY_TEMPLATES = TypeAdapter(dict[str, ActivityTemplate])
_NODE_PRIORITIES = TypeAdapter(dict[str, WorkItemPriority])
_MAIL_BINDINGS = TypeAdapter(dict[str, MailBinding])
_TIME_CONSTRAINTS = TypeAdapter(dict[str, TimeConstraint])
_ESCALATION_POLICIES = TypeAdapter(dict[str, EscalationPolicy])

#: BPMN 2.0 semantic model namespace (OMG / ISO 19510).
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
#: BPMN diagram interchange namespaces (shapes, edges, geometry).
BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS = "http://www.omg.org/spec/DD/20100524/DI"
#: XML Schema instance namespace (for conditionExpression xsi:type).
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
#: ProcWorks extension namespace, used to round-trip the structured XOR branch
#: partition (K7) -- which standard BPMN cannot express -- via extensionElements.
PROCWORKS_NS = "https://procworks/bpmn/ext"
#: Target namespace of exported definitions.
TARGET_NS = "https://procworks/bpmn"


class BpmnError(ValueError):
    """Raised when a BPMN document cannot be mapped onto the checked subset."""


# --- export --------------------------------------------------------------

#: Internal node type -> BPMN element local name (split and join collapse onto
#: the same gateway element; the role is reconstructed from the degree).
_EXPORT_TAG: dict[NodeType, str] = {
    NodeType.START: "startEvent",
    NodeType.END: "endEvent",
    NodeType.ACTIVITY: "task",
    NodeType.SUBPROCESS: "callActivity",
    NodeType.AND_SPLIT: "parallelGateway",
    NodeType.AND_JOIN: "parallelGateway",
    NodeType.XOR_SPLIT: "exclusiveGateway",
    NodeType.XOR_JOIN: "exclusiveGateway",
    NodeType.LOOP_START: "exclusiveGateway",
    NodeType.LOOP_END: "exclusiveGateway",
}


def export_bpmn(schema: ProcessSchema) -> str:
    """Serialise a schema to semantic BPMN 2.0 XML *with* a drawn diagram.

    A ``SUBPROCESS`` node is exported as ``bpmn:callActivity`` (its bound target
    schema id as ``calledElement``); the I/O mapping is not carried by BPMN.

    A loop (K6) exports as the canonical gateway pattern: LOOP_START/LOOP_END
    become an ``exclusiveGateway`` pair and the -- internally never stored --
    loop-back edge is emitted as an extra ``sequenceFlow`` from the end to the
    start gateway, carrying the derived repeat predicate as its
    ``conditionExpression`` (the exit flow stays uncaptioned: on re-import the
    back flow is dropped, and only edges leaving an XOR split may carry a
    caption). The structured ``LoopDecision`` itself round-trips through the
    ProcWorks extension (Schleifen-Konzept §8, stage S3).

    Three things the export carries beyond the bare control flow, because a
    foreign tool is otherwise handed a document it cannot draw and a re-import
    loses half the model (Nachtest 2026-09-22, defect 4):

    * **Lanes** -- one ``bpmn:lane`` per distinct staff rule, named by
      :func:`~procworks.model.staff_rule_text`, so the document shows who does
      what. Lanes are pure presentation; on import they are ignored and the
      staff rules come back from the ProcWorks extension.
    * **Diagram interchange** -- a ``bpmndi:BPMNDiagram`` with a shape per node,
      an edge per flow and a band per lane (see :func:`_export_diagram`).
    * **The staffing and annotation layers** in the ProcWorks extension, so the
      round-trip keeps the staff rules, the organisation they resolve against,
      forms, times, priorities, escalations and mail bindings.

    The element order follows the BPMN XSD sequence of ``tProcess``
    (extensionElements, laneSet, flow elements), so a strict validator accepts
    the document.
    """

    ET.register_namespace("bpmn", BPMN_NS)
    ET.register_namespace("bpmndi", BPMNDI_NS)
    ET.register_namespace("dc", DC_NS)
    ET.register_namespace("di", DI_NS)
    ET.register_namespace("xsi", XSI_NS)
    ET.register_namespace("procworks", PROCWORKS_NS)
    definitions = ET.Element(
        f"{{{BPMN_NS}}}definitions",
        {"id": f"defs_{schema.id}", "targetNamespace": TARGET_NS},
    )
    process = ET.SubElement(
        definitions,
        f"{{{BPMN_NS}}}process",
        {"id": schema.id, "name": schema.name, "isExecutable": "true"},
    )
    # 1. extensionElements, 2. laneSet, 3. flow elements -- the XSD order.
    _export_procworks_model(process, schema)
    lanes = _export_lanes(process, schema)
    for node in schema.nodes.values():
        attrib = {"id": node.id}
        if node.label:
            attrib["name"] = node.label
        if node.type is NodeType.SUBPROCESS:
            binding = schema.sub_process_bindings.get(node.id)
            if binding is not None:
                attrib["calledElement"] = binding.target_schema_id
        ET.SubElement(process, f"{{{BPMN_NS}}}{_EXPORT_TAG[node.type]}", attrib)
    control_edges = [e for e in schema.edges if e.type is EdgeType.CONTROL]
    flows: list[tuple[str, str, str]] = []  # (flow id, source, target)
    for index, edge in enumerate(control_edges, start=1):
        flow_id = f"flow_{index}"
        flow = ET.SubElement(
            process,
            f"{{{BPMN_NS}}}sequenceFlow",
            {"id": flow_id, "sourceRef": edge.source, "targetRef": edge.target},
        )
        flows.append((flow_id, edge.source, edge.target))
        if edge.condition:
            condition = ET.SubElement(
                flow,
                f"{{{BPMN_NS}}}conditionExpression",
                {f"{{{XSI_NS}}}type": "bpmn:tFormalExpression"},
            )
            condition.text = edge.condition
    flows.extend(_export_loop_back_flows(process, schema))
    _export_diagram(definitions, schema, flows, lanes)
    ET.indent(definitions)
    return ET.tostring(definitions, encoding="unicode", xml_declaration=True)


def _lane_key(schema: ProcessSchema, node_id: str) -> str | None:
    """Lane a node belongs to: its staff rule's canonical form, or ``None``.

    Nodes without a staff rule (events, gateways, automatic steps) belong to no
    lane -- BPMN permits that, and inventing a catch-all lane would claim an
    assignment the model does not make.
    """

    rule = schema.staff_rules.get(node_id)
    return None if rule is None else rule.model_dump_json()


def _export_lanes(process: ET.Element, schema: ProcessSchema) -> list[tuple[str, str, list[str]]]:
    """Emit one ``bpmn:lane`` per distinct staff rule; return the lane model.

    The order is the order in which the rules first appear along the node
    order, so the lanes read like the process. Returns ``(lane id, name, node
    ids)`` triples for the diagram; an empty list when the model assigns nobody
    (then no ``laneSet`` is written at all).
    """

    order: list[str] = []
    members: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for node_id in schema.nodes:
        key = _lane_key(schema, node_id)
        if key is None:
            continue
        if key not in members:
            order.append(key)
            members[key] = []
            names[key] = staff_rule_text(schema.staff_rules[node_id], schema)
        members[key].append(node_id)
    if not order:
        return []
    lane_set = ET.SubElement(
        process, f"{{{BPMN_NS}}}laneSet", {"id": f"lanes_{schema.id}"}
    )
    lanes: list[tuple[str, str, list[str]]] = []
    for index, key in enumerate(order, start=1):
        lane_id = f"lane_{index}"
        lane = ET.SubElement(
            lane_set, f"{{{BPMN_NS}}}lane", {"id": lane_id, "name": names[key]}
        )
        for node_id in members[key]:
            ref = ET.SubElement(lane, f"{{{BPMN_NS}}}flowNodeRef")
            ref.text = node_id
        lanes.append((lane_id, names[key], members[key]))
    return lanes


def _export_loop_back_flows(
    process: ET.Element, schema: ProcessSchema
) -> list[tuple[str, str, str]]:
    """Emit the canonical loop-back sequenceFlow per loop pair (K6, S3).

    The back edge is never stored internally (the graph stays acyclic); in the
    interchange document it is what makes the exported gateway pair a real
    BPMN REPEAT-UNTIL loop. It carries the derived repeat predicate as caption
    so foreign tools and readers see the condition; on re-import the flow is
    recognised structurally and dropped again (:func:`_split_loop_back_flows`).

    Returns the emitted ``(flow id, source, target)`` triples so the diagram can
    draw them (they are routed backwards, below the nodes).
    """

    emitted: list[tuple[str, str, str]] = []
    starts = [
        nid for nid, n in schema.nodes.items() if n.type is NodeType.LOOP_START
    ]
    for index, start_id in enumerate(sorted(starts), start=1):
        try:
            end_id, _body = loop_block(schema, start_id)
        except ValueError:  # pragma: no cover - K6a guards released schemas
            continue
        emitted.append((f"loopflow_{index}", end_id, start_id))
        flow = ET.SubElement(
            process,
            f"{{{BPMN_NS}}}sequenceFlow",
            {
                "id": f"loopflow_{index}",
                "sourceRef": end_id,
                "targetRef": start_id,
            },
        )
        decision = schema.loop_decisions.get(end_id)
        if decision is None:  # pragma: no cover - K6b guards released schemas
            continue
        element = schema.data_elements.get(decision.discriminator)
        caption = loop_condition_text(
            element.name if element is not None else decision.discriminator,
            decision,
        )
        condition = ET.SubElement(
            flow,
            f"{{{BPMN_NS}}}conditionExpression",
            {f"{{{XSI_NS}}}type": "bpmn:tFormalExpression"},
        )
        condition.text = caption
    return emitted


def _export_procworks_model(process: ET.Element, schema: ProcessSchema) -> None:
    """Round-trip everything BPMN cannot express (data, staffing, annotations).

    Standard BPMN only carries control flow; the structured XOR partition (K7),
    the loop decisions (K6) and the typed discriminators live in a ProcWorks
    extension so an exported document re-imports to the very same,
    still-correct schema.

    **Staffing travels with it** (since the Nachtest 2026-09-22, defect 4): the
    staff rules *and* the organisation they resolve against. Rules alone would
    not survive -- the validating import would reject them under Z1/Z2 because
    the target has no matching roles, units or agents. The organisation is
    therefore embedded as the imported schema's own master data; the id of a
    shared model travels only as a hint (``org_model_id``), never as a link,
    because the importing installation need not know that model.

    Deliberately **not** carried: ``sub_process_bindings`` and
    ``follow_up_links``. They reference *other schemas* by id, which the
    importing installation generally does not have -- a dangling reference would
    fail validation and cost the whole import.
    """

    sync_edges = [e for e in schema.edges if e.type is EdgeType.SYNC]
    payload: dict[str, object] = {
        "data_elements": [e.model_dump(mode="json") for e in schema.data_elements.values()],
        "data_accesses": [a.model_dump(mode="json") for a in schema.data_accesses],
        "xor_decisions": {
            nid: d.model_dump(mode="json") for nid, d in schema.xor_decisions.items()
        },
        "loop_decisions": {
            nid: d.model_dump(mode="json") for nid, d in schema.loop_decisions.items()
        },
        # K4 sync edges are no BPMN sequence flows (ordering-only); they
        # round-trip through the extension like the structured decisions.
        "sync_edges": [
            {"source": e.source, "target": e.target} for e in sync_edges
        ],
        "forms": {nid: f.model_dump(mode="json") for nid, f in schema.forms.items()},
        "connectors": {
            cid: c.model_dump(mode="json") for cid, c in schema.connectors.items()
        },
        "staff_rules": {
            nid: r.model_dump(mode="json") for nid, r in schema.staff_rules.items()
        },
        "org_model": (
            schema.org_model.model_dump(mode="json")
            if (schema.org_model.roles or schema.org_model.org_units or schema.org_model.agents)
            else None
        ),
        "org_model_id": schema.org_model_id,
        "service_bindings": {
            nid: b.model_dump(mode="json") for nid, b in schema.service_bindings.items()
        },
        "activity_templates": {
            tid: t.model_dump(mode="json")
            for tid, t in schema.activity_templates.items()
        },
        "node_priorities": {
            nid: p.model_dump(mode="json") for nid, p in schema.node_priorities.items()
        },
        "mail_bindings": {
            nid: m.model_dump(mode="json") for nid, m in schema.mail_bindings.items()
        },
        "time_constraints": {
            nid: t.model_dump(mode="json") for nid, t in schema.time_constraints.items()
        },
        "escalation_policies": {
            nid: p.model_dump(mode="json")
            for nid, p in schema.escalation_policies.items()
        },
        "deadline_seconds": schema.deadline_seconds,
        "is_library_subprocess": schema.is_library_subprocess,
    }
    # Empty layers are left out entirely; a pure control-flow model therefore
    # stays a plain BPMN document without any extension element.
    payload = {key: value for key, value in payload.items() if value}
    if not payload:
        return
    extensions = ET.SubElement(process, f"{{{BPMN_NS}}}extensionElements")
    model = ET.SubElement(extensions, f"{{{PROCWORKS_NS}}}model")
    model.text = json.dumps(payload)


# --- diagram interchange (BPMNDI) ----------------------------------------
#
# A BPMN document without diagram interchange is semantically complete but
# *invisible*: foreign tools open it and show an empty canvas (Nachtest
# 2026-09-22, defect 4). The layout below is deliberately simple and
# deterministic -- the same schema always yields the same picture -- and it is
# presentation only: nothing here ever reaches the model, and the import ignores
# the whole diagram.

#: Shape size per node type, in the proportions BPMN tools expect.
_SHAPE_SIZE: dict[NodeType, tuple[int, int]] = {
    NodeType.START: (36, 36),
    NodeType.END: (36, 36),
    NodeType.ACTIVITY: (120, 70),
    NodeType.SUBPROCESS: (120, 70),
    NodeType.AND_SPLIT: (50, 50),
    NodeType.AND_JOIN: (50, 50),
    NodeType.XOR_SPLIT: (50, 50),
    NodeType.XOR_JOIN: (50, 50),
    NodeType.LOOP_START: (50, 50),
    NodeType.LOOP_END: (50, 50),
}
#: Width of the lane label strip, horizontal step per layer, vertical step per
#: row, and the outer margin -- all in the BPMN coordinate unit (px).
_LANE_LABEL_W = 30
_COL_W = 180
_ROW_H = 100
_MARGIN = 40


def _layer_of(schema: ProcessSchema) -> dict[str, int]:
    """Longest-path layering of the control flow (the x position of each node).

    The stored graph is acyclic (a loop's back edge is derived, never stored),
    so a topological pass suffices. A node that is not reachable from START --
    which a valid schema does not have, but a defensive layout must survive --
    keeps layer 0.
    """

    successors: dict[str, list[str]] = {nid: [] for nid in schema.nodes}
    indegree = {nid: 0 for nid in schema.nodes}
    for edge in schema.edges:
        if edge.type is not EdgeType.CONTROL:
            continue  # SYNC edges are ordering-only and are not drawn
        successors[edge.source].append(edge.target)
        indegree[edge.target] += 1
    layer = {nid: 0 for nid in schema.nodes}
    queue = [nid for nid, deg in indegree.items() if deg == 0]
    while queue:
        current = queue.pop(0)
        for target in successors[current]:
            layer[target] = max(layer[target], layer[current] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return layer


def _band_of(
    schema: ProcessSchema, lanes: list[tuple[str, str, list[str]]], layer: dict[str, int]
) -> dict[str, int]:
    """Assign every node the band (lane row) it is *drawn* in.

    Nodes with a staff rule belong to their lane. Events, gateways and automatic
    steps carry no assignment -- they inherit the band of the step they follow
    (or, at the start of the process, of the step they lead to), which is how a
    BPMN diagram is drawn by hand. Without this the spine zigzagged between a
    separate band and the lanes on every second node.

    The laneSet itself stays untouched by this: a lane claims only the steps the
    model really assigns. This is geometry, not a statement about who works.
    """

    band = {
        node_id: index
        for index, (_lane_id, _name, members) in enumerate(lanes)
        for node_id in members
    }
    if not lanes:
        return dict.fromkeys(schema.nodes, 0)
    by_layer = sorted(schema.nodes, key=lambda n: (layer[n], n))
    for node_id in by_layer:  # forwards: inherit from an earlier step
        if node_id in band:
            continue
        candidates = [
            band[edge.source] for edge in schema.incoming(node_id) if edge.source in band
        ]
        if candidates:
            band[node_id] = min(candidates)
    for node_id in reversed(by_layer):  # backwards: START and its like
        if node_id in band:
            continue
        candidates = [
            band[edge.target] for edge in schema.outgoing(node_id) if edge.target in band
        ]
        band[node_id] = min(candidates) if candidates else 0
    return band


def _place_nodes(
    schema: ProcessSchema, lanes: list[tuple[str, str, list[str]]]
) -> tuple[dict[str, tuple[float, float]], list[tuple[str, float, float]], float, float]:
    """Assign every node a centre point, grouped into lane bands.

    The layer gives the column, the band gives the lane, and within a band a node
    takes the first row whose column is still free -- so parallel branches stack
    instead of overlapping while the spine stays on one line.

    :returns: ``(centre per node, (lane id, y, height) per lane, width, height)``
    """

    layer = _layer_of(schema)
    columns = max(layer.values(), default=0) + 1
    band = _band_of(schema, lanes, layer)
    band_count = max(band.values(), default=0) + 1

    centres: dict[str, tuple[float, float]] = {}
    lane_boxes: list[tuple[str, float, float]] = []
    y = float(_MARGIN)
    for index in range(band_count):
        members = [nid for nid in schema.nodes if band[nid] == index]
        taken: set[tuple[int, int]] = set()
        rows = 1
        for node_id in sorted(members, key=lambda n: (layer[n], n)):
            column = layer[node_id]
            row = 0
            while (row, column) in taken:
                row += 1
            taken.add((row, column))
            rows = max(rows, row + 1)
            centres[node_id] = (
                _MARGIN + _LANE_LABEL_W + _COL_W * column + _COL_W / 2,
                y + _ROW_H * row + _ROW_H / 2,
            )
        height = _ROW_H * rows
        if index < len(lanes):
            lane_boxes.append((lanes[index][0], y, height))
        y += height
    width = _MARGIN + _LANE_LABEL_W + _COL_W * columns + _MARGIN
    return centres, lane_boxes, width, y


def _bounds(parent: ET.Element, x: float, y: float, w: float, h: float) -> None:
    """Append a ``dc:Bounds`` child with integer-rounded geometry."""

    ET.SubElement(
        parent,
        f"{{{DC_NS}}}Bounds",
        {"x": f"{x:.0f}", "y": f"{y:.0f}", "width": f"{w:.0f}", "height": f"{h:.0f}"},
    )


def _waypoint(parent: ET.Element, x: float, y: float) -> None:
    """Append a ``di:waypoint`` to a ``BPMNEdge``."""

    ET.SubElement(parent, f"{{{DI_NS}}}waypoint", {"x": f"{x:.0f}", "y": f"{y:.0f}"})


def _route(
    source: tuple[float, float, int, int],
    target: tuple[float, float, int, int],
    floor: float,
) -> list[tuple[float, float]]:
    """Orthogonal waypoints between two shapes (centre + size each).

    Forward flows leave the source on the right and enter the target on the
    left, with one vertical jog in between when the rows differ. A **backward**
    flow -- only the derived loop-back edge is one -- is routed below everything
    (``floor``), which is how a REPEAT-UNTIL loop is drawn by hand as well.
    """

    sx, sy, sw, sh = source
    tx, ty, tw, th = target
    if tx > sx:
        start, end = (sx + sw / 2, sy), (tx - tw / 2, ty)
        if abs(sy - ty) < 1:
            return [start, end]
        middle = (start[0] + end[0]) / 2
        return [start, (middle, sy), (middle, ty), end]
    low = floor + _ROW_H / 2
    return [(sx, sy + sh / 2), (sx, low), (tx, low), (tx, ty + th / 2)]


def _export_diagram(
    definitions: ET.Element,
    schema: ProcessSchema,
    flows: list[tuple[str, str, str]],
    lanes: list[tuple[str, str, list[str]]],
) -> None:
    """Emit the ``bpmndi:BPMNDiagram`` for the exported process.

    One shape per node, one band per lane and one edge per sequence flow
    (including the derived loop-back flows, so a loop is visible as such).
    """

    centres, lane_boxes, width, height = _place_nodes(schema, lanes)
    diagram = ET.SubElement(
        definitions, f"{{{BPMNDI_NS}}}BPMNDiagram", {"id": f"diagram_{schema.id}"}
    )
    plane = ET.SubElement(
        diagram,
        f"{{{BPMNDI_NS}}}BPMNPlane",
        {"id": f"plane_{schema.id}", "bpmnElement": schema.id},
    )
    for lane_id, y, lane_height in lane_boxes:
        shape = ET.SubElement(
            plane,
            f"{{{BPMNDI_NS}}}BPMNShape",
            {"id": f"shape_{lane_id}", "bpmnElement": lane_id, "isHorizontal": "true"},
        )
        _bounds(shape, _MARGIN, y, width - 2 * _MARGIN, lane_height)
    for node_id, node in schema.nodes.items():
        cx, cy = centres[node_id]
        w, h = _SHAPE_SIZE[node.type]
        shape = ET.SubElement(
            plane,
            f"{{{BPMNDI_NS}}}BPMNShape",
            {"id": f"shape_{node_id}", "bpmnElement": node_id},
        )
        _bounds(shape, cx - w / 2, cy - h / 2, w, h)
    for flow_id, source_id, target_id in flows:
        source_node, target_node = schema.nodes[source_id], schema.nodes[target_id]
        edge = ET.SubElement(
            plane,
            f"{{{BPMNDI_NS}}}BPMNEdge",
            {"id": f"edge_{flow_id}", "bpmnElement": flow_id},
        )
        points = _route(
            (*centres[source_id], *_SHAPE_SIZE[source_node.type]),
            (*centres[target_id], *_SHAPE_SIZE[target_node.type]),
            height,
        )
        for x, y in points:
            _waypoint(edge, x, y)


# --- import --------------------------------------------------------------

#: BPMN flow-node local names that map onto an ACTIVITY.
_ACTIVITY_TAGS = frozenset(
    {
        "task",
        "userTask",
        "serviceTask",
        "manualTask",
        "scriptTask",
        "businessRuleTask",
        "sendTask",
        "receiveTask",
    }
)
#: BPMN gateway local names whose split/join role is inferred from the degree.
_GATEWAY_TAGS = frozenset({"parallelGateway", "exclusiveGateway"})
#: Non-flow metadata elements that are ignored on import.
_IGNORED_TAGS = frozenset(
    {
        "documentation",
        "extensionElements",
        "laneSet",
        "ioSpecification",
        "property",
        "dataObject",
        "dataObjectReference",
        "textAnnotation",
        "association",
        "group",
    }
)


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_process(root: ET.Element) -> ET.Element:
    for child in root.iter():
        if _localname(child.tag) == "process":
            return child
    raise BpmnError("no <process> element found in the BPMN document")


def _condition_of(flow: ET.Element) -> str | None:
    for child in flow:
        if _localname(child.tag) == "conditionExpression":
            text = (child.text or "").strip()
            return text or None
    return None


def _sync_edges_of(process: ET.Element) -> list[dict[str, str]]:
    """Parse the K4 sync-edge list from the ProcWorks extension (or [])."""

    model = _procworks_model_of(process)
    entries = model.get("sync_edges", [])
    result: list[dict[str, str]] = []
    if isinstance(entries, list):
        for entry in entries:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("source"), str)
                and isinstance(entry.get("target"), str)
            ):
                result.append({"source": entry["source"], "target": entry["target"]})
    return result


def _procworks_model_of(process: ET.Element) -> dict[str, object]:
    """Parse the ProcWorks data-layer extension (elements, accesses, K7)."""

    for descendant in process.iter():
        if _localname(descendant.tag) == "model":
            text = (descendant.text or "").strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise BpmnError(f"invalid procworks:model payload: {exc}") from exc
            if not isinstance(parsed, dict):
                raise BpmnError("procworks:model payload must be an object")
            return parsed
    return {}


def import_bpmn(
    xml: str,
    *,
    schema_id: str | None = None,
    name: str | None = None,
    resolver: SchemaResolver | None = None,
) -> ProcessSchema:
    """Map a BPMN 2.0 document onto a validated block-structured schema.

    Raises ``BpmnError`` for malformed XML or constructs outside the checked
    subset, and ``CorrectnessError`` if the mapped graph is not block-structured
    (the import is validated before it is returned -- no bypass).
    """

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise BpmnError(f"invalid BPMN XML: {exc}") from exc

    process = _find_process(root)
    raw_nodes: dict[str, tuple[str, str]] = {}
    flows: list[tuple[str, str, str | None]] = []
    for child in process:
        local = _localname(child.tag)
        if local == "sequenceFlow":
            source = child.get("sourceRef")
            target = child.get("targetRef")
            if not source or not target:
                raise BpmnError("sequenceFlow is missing sourceRef/targetRef")
            flows.append((source, target, _condition_of(child)))
        elif local in _ACTIVITY_TAGS or local in _GATEWAY_TAGS or local in {
            "startEvent",
            "endEvent",
        }:
            node_id = child.get("id")
            if not node_id:
                raise BpmnError(f"<{local}> is missing its id")
            raw_nodes[node_id] = (local, child.get("name") or "")
        elif local in _IGNORED_TAGS:
            continue
        else:
            raise BpmnError(f"unsupported BPMN element '{local}'")

    for source, target, _ in flows:
        if source not in raw_nodes or target not in raw_nodes:
            raise BpmnError("sequenceFlow references an unknown flow node")

    # Loop recognition must run before node-type resolution: after dropping a
    # loop-back flow both gateways have in=1/out=1, which is neither a pure
    # split nor a pure join.
    flows, loop_roles = _split_loop_back_flows(raw_nodes, flows)

    indegree = {nid: 0 for nid in raw_nodes}
    outdegree = {nid: 0 for nid in raw_nodes}
    for source, target, _ in flows:
        outdegree[source] += 1
        indegree[target] += 1

    nodes: dict[str, Node] = {}
    for node_id, (local, label) in raw_nodes.items():
        node_type = loop_roles.get(node_id) or _resolve_node_type(
            local, indegree[node_id], outdegree[node_id]
        )
        nodes[node_id] = Node(id=node_id, type=node_type, label=label)

    edges = [
        ControlEdge(source=s, target=t, type=EdgeType.CONTROL, condition=c)
        for s, t, c in flows
    ]
    for entry in _sync_edges_of(process):
        edges.append(
            ControlEdge(
                source=entry["source"], target=entry["target"], type=EdgeType.SYNC
            )
        )
    model = _procworks_model_of(process)
    data_elements = {
        e.id: e for e in _DATA_ELEMENTS.validate_python(model.get("data_elements", []))
    }
    data_accesses = _DATA_ACCESSES.validate_python(model.get("data_accesses", []))
    xor_decisions = _XOR_DECISIONS.validate_python(model.get("xor_decisions", {}))
    loop_decisions = _LOOP_DECISIONS.validate_python(model.get("loop_decisions", {}))
    forms = _FORMS.validate_python(model.get("forms", {}))
    connectors = _CONNECTORS.validate_python(model.get("connectors", {}))
    # Staffing and the annotation layers (see _export_procworks_model). Absent
    # keys are the normal case for a foreign document and for anything exported
    # before these layers travelled -- every one of them defaults to empty.
    staff_rules = _STAFF_RULES.validate_python(model.get("staff_rules", {}))
    org_model = _ORG_MODEL.validate_python(model.get("org_model") or {})
    schema = ProcessSchema(
        id=schema_id or process.get("id") or "imported",
        name=name or process.get("name") or "Imported",
        nodes=nodes,
        edges=edges,
        data_elements=data_elements,
        data_accesses=data_accesses,
        xor_decisions=xor_decisions,
        loop_decisions=loop_decisions,
        forms=forms,
        connectors=connectors,
        staff_rules=staff_rules,
        # The organisation comes along as the imported schema's OWN master data
        # (org_model_id stays unset): the importing installation need not know
        # the shared model the export came from, and a dangling link would fail
        # validation. Linking it to a shared model afterwards is a normal,
        # validated operation (link_org_model).
        org_model=org_model,
        service_bindings=_SERVICE_BINDINGS.validate_python(
            model.get("service_bindings", {})
        ),
        activity_templates=_ACTIVITY_TEMPLATES.validate_python(
            model.get("activity_templates", {})
        ),
        node_priorities=_NODE_PRIORITIES.validate_python(
            model.get("node_priorities", {})
        ),
        mail_bindings=_MAIL_BINDINGS.validate_python(model.get("mail_bindings", {})),
        time_constraints=_TIME_CONSTRAINTS.validate_python(
            model.get("time_constraints", {})
        ),
        escalation_policies=_ESCALATION_POLICIES.validate_python(
            model.get("escalation_policies", {})
        ),
        deadline_seconds=_optional_float(model.get("deadline_seconds")),
        is_library_subprocess=bool(model.get("is_library_subprocess", False)),
    )
    return raise_if_invalid(schema, resolver)


def _optional_float(value: object) -> float | None:
    """Read an optional number from the extension payload (None when absent)."""

    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _split_loop_back_flows(
    raw_nodes: dict[str, tuple[str, str]],
    flows: list[tuple[str, str, str | None]],
) -> tuple[list[tuple[str, str, str | None]], dict[str, NodeType]]:
    """Recognise canonical loop-back flows and retype their gateway pair (K6).

    A flow ``s -> t`` is a loop-back edge iff

    * both endpoints are ``exclusiveGateway`` elements,
    * ``s`` has in=1/out=2 (the loop end) and ``t`` has in=2/out=1 (the loop
      start), and
    * **without this flow, ``t`` still reaches ``s``** -- the body path from
      the start down to the end. This reachability test is what tells a real
      loop apart from an *empty XOR branch*: there the shape-identical
      ``split -> join`` flow points forward, and the join can never reach the
      split again in the remaining DAG.

    Every recognised back flow is removed (the internal graph must stay
    acyclic -- the loop-back is derived, never stored) and its endpoints are
    pinned to LOOP_END/LOOP_START. Nested loops just yield several disjoint
    pairs. The structured ``LoopDecision`` is *not* reconstructed here -- it
    comes from the ProcWorks extension; a recognised loop without one fails
    K6b in the validating import (never stored undecidable).
    """

    indegree = {nid: 0 for nid in raw_nodes}
    outdegree = {nid: 0 for nid in raw_nodes}
    for source, target, _ in flows:
        outdegree[source] += 1
        indegree[target] += 1

    def is_exclusive(node_id: str) -> bool:
        return raw_nodes[node_id][0] == "exclusiveGateway"

    def reaches(origin: str, goal: str, skipped: tuple[str, str, str | None]) -> bool:
        succ: dict[str, list[str]] = {}
        for flow in flows:
            if flow is skipped:
                continue
            succ.setdefault(flow[0], []).append(flow[1])
        seen = {origin}
        frontier = [origin]
        while frontier:
            current = frontier.pop()
            if current == goal:
                return True
            for nxt in succ.get(current, []):
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        return False

    loop_roles: dict[str, NodeType] = {}
    kept: list[tuple[str, str, str | None]] = []
    for flow in flows:
        source, target, _condition = flow
        if (
            is_exclusive(source)
            and is_exclusive(target)
            and indegree[source] == 1
            and outdegree[source] == 2
            and indegree[target] == 2
            and outdegree[target] == 1
            and source not in loop_roles
            and target not in loop_roles
            and reaches(target, source, flow)
        ):
            loop_roles[source] = NodeType.LOOP_END
            loop_roles[target] = NodeType.LOOP_START
            continue  # drop the back flow -- it is derived, never stored
        kept.append(flow)
    return kept, loop_roles


def _resolve_node_type(local: str, indegree: int, outdegree: int) -> NodeType:
    if local == "startEvent":
        return NodeType.START
    if local == "endEvent":
        return NodeType.END
    if local in _ACTIVITY_TAGS:
        return NodeType.ACTIVITY
    # A gateway is either a pure split (one in, many out) or a pure join.
    is_parallel = local == "parallelGateway"
    if outdegree >= 2 and indegree <= 1:
        return NodeType.AND_SPLIT if is_parallel else NodeType.XOR_SPLIT
    if indegree >= 2 and outdegree <= 1:
        return NodeType.AND_JOIN if is_parallel else NodeType.XOR_JOIN
    raise BpmnError(
        f"gateway is neither a pure split nor a pure join "
        f"(in={indegree}, out={outdegree}); only block-structured gateways are supported"
    )
