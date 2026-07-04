# SPDX-License-Identifier: BUSL-1.1
"""Ad-hoc changes on a single running instance (roadmap step 10).

An ad-hoc change adapts *one* instance without touching the released schema:
the instance gets its own schema variant (``ad_hoc_schema``) plus an updated
node/edge marking. Every change is checked against:

  * R1 (state compatibility): only the not-yet-executed region may change -- a
    node that is already RUNNING/COMPLETED/SKIPPED is frozen;
  * R2 (correctness preservation): the resulting schema still satisfies all
    structural and data-flow rules (validate-before-commit, as for normal
    change operations).

The executed nodes keep their ids, so the existing markings stay valid and the
Execution Engine continues seamlessly against the variant.
"""

from __future__ import annotations

from procworks.model import (
    ControlEdge,
    EdgeState,
    LifecycleState,
    Node,
    NodeState,
    NodeType,
    ProcessInstance,
    ProcessSchema,
)
from procworks.validator import (
    CorrectnessError,
    SchemaResolver,
    ValidationFinding,
    raise_if_invalid,
)


def _edge_key(source: str, target: str) -> str:
    return f"{source}->{target}"


def effective_schema(
    instance: ProcessInstance, base_schema: ProcessSchema
) -> ProcessSchema:
    """Return the schema this instance actually runs against (variant or base)."""

    return instance.ad_hoc_schema or base_schema


def _r1_error(message: str, node_id: str | None = None) -> CorrectnessError:
    return CorrectnessError([ValidationFinding(rule="R1", message=message, node_id=node_id)])


def adhoc_insert_activity(
    instance: ProcessInstance,
    schema: ProcessSchema,
    after_node_id: str,
    label: str,
    *,
    resolver: SchemaResolver | None = None,
    new_node_id: str | None = None,
) -> ProcessInstance:
    """Insert a serial ACTIVITY after ``after_node_id`` into one instance.

    requires (R1): the anchor exists and is not END, its single outgoing edge
                   is not yet signaled and its successor is NOT_ACTIVATED (the
                   region is still ahead of the execution front).
    ensures (R2):  the resulting instance schema is still correct; the new node
                   is spliced in as NOT_ACTIVATED.
    """

    current = effective_schema(instance, schema)
    anchor = current.nodes.get(after_node_id)
    if anchor is None:
        raise _r1_error(f"node '{after_node_id}' does not exist", after_node_id)
    if anchor.type is NodeType.END:
        raise _r1_error("cannot insert after END", after_node_id)
    outgoing = current.outgoing(after_node_id)
    if len(outgoing) != 1:
        raise _r1_error(
            f"anchor '{after_node_id}' must have exactly one outgoing edge",
            after_node_id,
        )
    edge = outgoing[0]
    successor_id = edge.target
    edge_state = instance.edge_states.get(_edge_key(after_node_id, successor_id))
    if edge_state is not None and edge_state is not EdgeState.NOT_SIGNALED:
        raise _r1_error(
            f"edge '{after_node_id}->{successor_id}' is already signaled", after_node_id
        )
    if instance.node_states.get(successor_id) not in (None, NodeState.NOT_ACTIVATED):
        raise _r1_error(
            f"successor '{successor_id}' is already reached", successor_id
        )

    candidate = current.model_copy(deep=True)
    new_node = Node(
        id=new_node_id or _free_id(candidate, "adhoc"),
        type=NodeType.ACTIVITY,
        label=label,
    )
    candidate.nodes[new_node.id] = new_node
    candidate.edges = [
        e
        for e in candidate.edges
        if not (e.source == after_node_id and e.target == successor_id)
    ]
    candidate.edges.append(ControlEdge(source=after_node_id, target=new_node.id))
    candidate.edges.append(ControlEdge(source=new_node.id, target=successor_id))
    candidate.lifecycle_state = LifecycleState.RELEASED
    raise_if_invalid(candidate, resolver)

    result = instance.model_copy(deep=True)
    result.ad_hoc_schema = candidate
    result.node_states[new_node.id] = NodeState.NOT_ACTIVATED
    result.edge_states.pop(_edge_key(after_node_id, successor_id), None)
    result.edge_states[_edge_key(after_node_id, new_node.id)] = EdgeState.NOT_SIGNALED
    result.edge_states[_edge_key(new_node.id, successor_id)] = EdgeState.NOT_SIGNALED
    result.ad_hoc_deltas.append(
        f"insert {new_node.id} ('{label}') after {after_node_id}"
    )
    return result


def adhoc_delete_node(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    *,
    resolver: SchemaResolver | None = None,
) -> ProcessInstance:
    """Remove a not-yet-reached serial ACTIVITY from one instance.

    requires (R1): the node exists, is an ACTIVITY, is still NOT_ACTIVATED and
                   sits on a serial stretch (exactly one predecessor and one
                   successor).
    ensures (R2):  predecessor and successor are reconnected and the resulting
                   instance schema is still correct.
    """

    current = effective_schema(instance, schema)
    node = current.nodes.get(node_id)
    if node is None:
        raise _r1_error(f"node '{node_id}' does not exist", node_id)
    if node.type is not NodeType.ACTIVITY:
        raise _r1_error("only ACTIVITY nodes can be deleted ad-hoc", node_id)
    if instance.node_states.get(node_id) is not NodeState.NOT_ACTIVATED:
        raise _r1_error(f"node '{node_id}' is already reached", node_id)
    incoming = current.incoming(node_id)
    outgoing = current.outgoing(node_id)
    if len(incoming) != 1 or len(outgoing) != 1:
        raise _r1_error(
            f"node '{node_id}' is not on a serial stretch (one in/one out)", node_id
        )
    predecessor_id = incoming[0].source
    successor_id = outgoing[0].target

    candidate = current.model_copy(deep=True)
    del candidate.nodes[node_id]
    candidate.edges = [
        e for e in candidate.edges if e.source != node_id and e.target != node_id
    ]
    candidate.data_accesses = [
        a for a in candidate.data_accesses if a.node_id != node_id
    ]
    candidate.staff_rules.pop(node_id, None)
    candidate.service_bindings.pop(node_id, None)
    candidate.edges.append(ControlEdge(source=predecessor_id, target=successor_id))
    candidate.lifecycle_state = LifecycleState.RELEASED
    raise_if_invalid(candidate, resolver)

    result = instance.model_copy(deep=True)
    result.ad_hoc_schema = candidate
    result.node_states.pop(node_id, None)
    result.edge_states.pop(_edge_key(predecessor_id, node_id), None)
    result.edge_states.pop(_edge_key(node_id, successor_id), None)
    result.edge_states[_edge_key(predecessor_id, successor_id)] = EdgeState.NOT_SIGNALED
    result.ad_hoc_deltas.append(f"delete {node_id}")
    return result


def adhoc_rename_activity(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    label: str,
    *,
    resolver: SchemaResolver | None = None,
) -> ProcessInstance:
    """Relabel a not-yet-reached ACTIVITY/SUBPROCESS in one instance.

    A relabelling is the smallest possible adaptation to reality: it never
    touches structure, markings or data flow, so it always preserves R2. R1
    still restricts it to the not-yet-executed region, so the recorded history
    of already reached steps is never rewritten.

    requires (R1): the node exists, is an ACTIVITY or SUBPROCESS and is still
                   NOT_ACTIVATED (not yet reached by the execution front).
    ensures (R2):  the instance schema stays correct; the markings are kept.
    """

    current = effective_schema(instance, schema)
    node = current.nodes.get(node_id)
    if node is None:
        raise _r1_error(f"node '{node_id}' does not exist", node_id)
    if node.type not in (NodeType.ACTIVITY, NodeType.SUBPROCESS):
        raise _r1_error(
            "only ACTIVITY or SUBPROCESS nodes can be renamed ad-hoc", node_id
        )
    if instance.node_states.get(node_id) not in (None, NodeState.NOT_ACTIVATED):
        raise _r1_error(f"node '{node_id}' is already reached", node_id)

    candidate = current.model_copy(deep=True)
    candidate.nodes[node_id].label = label
    candidate.lifecycle_state = LifecycleState.RELEASED
    raise_if_invalid(candidate, resolver)

    result = instance.model_copy(deep=True)
    result.ad_hoc_schema = candidate
    result.ad_hoc_deltas.append(f"rename {node_id} to '{label}'")
    return result


def _free_id(schema: ProcessSchema, prefix: str) -> str:
    i = 1
    while f"{prefix}_{i}" in schema.nodes:
        i += 1
    return f"{prefix}_{i}"
