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
    Node,
    NodeType,
    ProcessSchema,
    XorDecision,
)
from procworks.validator import SchemaResolver, raise_if_invalid

_DATA_ELEMENTS = TypeAdapter(list[DataElement])
_DATA_ACCESSES = TypeAdapter(list[DataAccess])
_XOR_DECISIONS = TypeAdapter(dict[str, XorDecision])
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
}


def export_bpmn(schema: ProcessSchema) -> str:
    """Serialise a schema to semantic BPMN 2.0 XML.

    A ``SUBPROCESS`` node is exported as ``bpmn:callActivity`` (its bound target
    schema id as ``calledElement``); the I/O mapping is not carried by BPMN.
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
    for index, edge in enumerate(schema.edges, start=1):
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
    _export_procworks_model(process, schema)
    ET.indent(definitions)
    return ET.tostring(definitions, encoding="unicode", xml_declaration=True)


def _export_procworks_model(process: ET.Element, schema: ProcessSchema) -> None:
    """Round-trip the data layer BPMN cannot express (elements, accesses, K7).

    Standard BPMN only carries control flow; the structured XOR partition (K7)
    and its typed discriminator live in a ProcWorks extension so an exported
    document re-imports to the very same, still-correct schema.
    """

    if not (
        schema.data_elements
        or schema.data_accesses
        or schema.xor_decisions
        or schema.forms
        or schema.connectors
    ):
        return
    extensions = ET.SubElement(process, f"{{{BPMN_NS}}}extensionElements")
    payload = {
        "data_elements": [e.model_dump(mode="json") for e in schema.data_elements.values()],
        "data_accesses": [a.model_dump(mode="json") for a in schema.data_accesses],
        "xor_decisions": {
            nid: d.model_dump(mode="json") for nid, d in schema.xor_decisions.items()
        },
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

    indegree = {nid: 0 for nid in raw_nodes}
    outdegree = {nid: 0 for nid in raw_nodes}
    for source, target, _ in flows:
        if source not in raw_nodes or target not in raw_nodes:
            raise BpmnError("sequenceFlow references an unknown flow node")
        outdegree[source] += 1
        indegree[target] += 1

    nodes: dict[str, Node] = {}
    for node_id, (local, label) in raw_nodes.items():
        node_type = _resolve_node_type(local, indegree[node_id], outdegree[node_id])
        nodes[node_id] = Node(id=node_id, type=node_type, label=label)

    edges = [
        ControlEdge(source=s, target=t, type=EdgeType.CONTROL, condition=c)
        for s, t, c in flows
    ]
    model = _procworks_model_of(process)
    data_elements = {
        e.id: e for e in _DATA_ELEMENTS.validate_python(model.get("data_elements", []))
    }
    data_accesses = _DATA_ACCESSES.validate_python(model.get("data_accesses", []))
    xor_decisions = _XOR_DECISIONS.validate_python(model.get("xor_decisions", {}))
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
        forms=forms,
        connectors=connectors,
    )
    return raise_if_invalid(schema, resolver)


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
