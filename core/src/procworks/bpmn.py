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

The mapping uses semantic BPMN only (no diagram interchange / layout):

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
    ConnectorDescriptor,
    ControlEdge,
    DataAccess,
    DataElement,
    EdgeType,
    Form,
    LoopDecision,
    Node,
    NodeType,
    ProcessSchema,
    XorDecision,
    loop_block,
    loop_condition_text,
)
from procworks.validator import SchemaResolver, raise_if_invalid

_DATA_ELEMENTS = TypeAdapter(list[DataElement])
_DATA_ACCESSES = TypeAdapter(list[DataAccess])
_XOR_DECISIONS = TypeAdapter(dict[str, XorDecision])
_LOOP_DECISIONS = TypeAdapter(dict[str, LoopDecision])
_FORMS = TypeAdapter(dict[str, Form])
_CONNECTORS = TypeAdapter(dict[str, ConnectorDescriptor])

#: BPMN 2.0 semantic model namespace (OMG / ISO 19510).
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
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
    """Serialise a schema to semantic BPMN 2.0 XML.

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
    """

    ET.register_namespace("bpmn", BPMN_NS)
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
    for index, edge in enumerate(control_edges, start=1):
        flow = ET.SubElement(
            process,
            f"{{{BPMN_NS}}}sequenceFlow",
            {"id": f"flow_{index}", "sourceRef": edge.source, "targetRef": edge.target},
        )
        if edge.condition:
            condition = ET.SubElement(
                flow,
                f"{{{BPMN_NS}}}conditionExpression",
                {f"{{{XSI_NS}}}type": "bpmn:tFormalExpression"},
            )
            condition.text = edge.condition
    _export_loop_back_flows(process, schema)
    _export_procworks_model(process, schema)
    ET.indent(definitions)
    return ET.tostring(definitions, encoding="unicode", xml_declaration=True)


def _export_loop_back_flows(process: ET.Element, schema: ProcessSchema) -> None:
    """Emit the canonical loop-back sequenceFlow per loop pair (K6, S3).

    The back edge is never stored internally (the graph stays acyclic); in the
    interchange document it is what makes the exported gateway pair a real
    BPMN REPEAT-UNTIL loop. It carries the derived repeat predicate as caption
    so foreign tools and readers see the condition; on re-import the flow is
    recognised structurally and dropped again (:func:`_split_loop_back_flows`).
    """

    starts = [
        nid for nid, n in schema.nodes.items() if n.type is NodeType.LOOP_START
    ]
    for index, start_id in enumerate(sorted(starts), start=1):
        try:
            end_id, _body = loop_block(schema, start_id)
        except ValueError:  # pragma: no cover - K6a guards released schemas
            continue
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


def _export_procworks_model(process: ET.Element, schema: ProcessSchema) -> None:
    """Round-trip the data layer BPMN cannot express (elements, accesses, K7/K6).

    Standard BPMN only carries control flow; the structured XOR partition (K7),
    the loop decisions (K6) and the typed discriminators live in a ProcWorks
    extension so an exported document re-imports to the very same,
    still-correct schema.
    """

    sync_edges = [e for e in schema.edges if e.type is EdgeType.SYNC]
    if not (
        schema.data_elements
        or schema.data_accesses
        or schema.xor_decisions
        or schema.loop_decisions
        or schema.forms
        or schema.connectors
        or sync_edges
    ):
        return
    extensions = ET.SubElement(process, f"{{{BPMN_NS}}}extensionElements")
    payload = {
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
    }
    model = ET.SubElement(extensions, f"{{{PROCWORKS_NS}}}model")
    model.text = json.dumps(payload)


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
    )
    return raise_if_invalid(schema, resolver)


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
