# SPDX-License-Identifier: BUSL-1.1
"""Execution Engine (roadmap step 8).

Instantiates a RELEASED schema into a ProcessInstance and drives it through the
ADEPT-style node/edge marking semantics:

  * node marking (NS):  NOT_ACTIVATED -> ACTIVATED -> RUNNING -> COMPLETED,
                        or NOT_ACTIVATED -> SKIPPED;
  * edge marking (ES):  NOT_SIGNALED -> TRUE_SIGNALED | FALSE_SIGNALED.

Gateways and the start node complete automatically once activated; ACTIVITY
nodes wait for interactive work (start_activity / complete_activity). An
XOR_SPLIT resolves its outgoing branch automatically from the instance data via
its structured, K7-valid decision (no manual choice). The marking propagation
is the runtime counterpart of the structural correctness rules, so under any
reachable end marking every node is COMPLETED or SKIPPED.

A SUBPROCESS node is handled by composition: with an ExecutionContext it spawns
a child instance of its pinned target schema (passing the bound input data),
stays RUNNING while the child runs, and on the child's completion writes the
mapped output back into the parent before advancing. Without a context the
SUBPROCESS node completes immediately as an opaque black box.

When an instance completes, its ASYNC, ON_COMPLETE follow-up links each start a
new, fully decoupled top-level instance of the follow-up target (F3), seeded
with the handover-mapped data.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from procworks import assignment
from procworks.conditions import ConditionError, evaluate_condition
from procworks.model import (
    ControlEdge,
    EdgeState,
    FollowUpLink,
    FollowUpMode,
    FollowUpTrigger,
    InstanceState,
    LifecycleState,
    Node,
    NodeState,
    NodeType,
    ProcessInstance,
    ProcessSchema,
    SubProcessBinding,
    resolve_xor_target,
)
from procworks.store import InstanceStore
from procworks.validator import SchemaResolver

_instance_counter = itertools.count(1)


@dataclass
class ExecutionContext:
    """Wiring the engine needs to run composed (sub-/follow-up) processes.

    ``resolver`` resolves a pinned target schema (composition rules H1-H4) and
    ``instances`` persists the spawned child instances. When no context is
    given the engine treats a SUBPROCESS node as an opaque black box that
    completes immediately (the step 8 behaviour).
    """

    resolver: SchemaResolver
    instances: InstanceStore

#: Non-activity node types that complete automatically once activated. An
#: XOR_SPLIT is handled separately in ``_advance`` (it auto-resolves its branch
#: from the instance data, K7); END is excluded because it terminates the
#: instance.
_AUTO_COMPLETE = frozenset(
    {
        NodeType.START,
        NodeType.AND_SPLIT,
        NodeType.AND_JOIN,
        NodeType.XOR_JOIN,
    }
)


class ExecutionError(Exception):
    """Raised when a runtime operation is not allowed in the current state."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _new_instance_id() -> str:
    return f"instance_{next(_instance_counter)}"


def _edge_key(edge: ControlEdge) -> str:
    return f"{edge.source}->{edge.target}"


def instantiate(
    schema: ProcessSchema,
    instance_id: str | None = None,
    *,
    context: ExecutionContext | None = None,
    parent_instance_id: str | None = None,
    parent_node_id: str | None = None,
    initial_data: dict[str, object] | None = None,
    allow_unreleased: bool = False,
    is_test: bool = False,
) -> ProcessInstance:
    """Create a running instance of a RELEASED schema.

    requires: schema is RELEASED (unless ``allow_unreleased`` is set).
    ensures:  all nodes NOT_ACTIVATED / all edges NOT_SIGNALED, then the start
              node is activated and the markings are advanced to the first
              activities (or the end).

    With a context the instance is persisted in the instance store and any
    SUBPROCESS node reached during the advance spawns its child instance.
    ``initial_data`` seeds the process variables (used for sub-process input
    mappings); ``parent_instance_id`` / ``parent_node_id`` link a child back to
    the SUBPROCESS node that spawned it.

    ``allow_unreleased`` lets a modeller/admin spin up a *test* instance of a
    draft schema (the structure is still CbC-validated); set ``is_test`` so the
    instance is flagged and kept out of the monitoring KPIs.
    """

    if schema.lifecycle_state is not LifecycleState.RELEASED and not allow_unreleased:
        raise ExecutionError(
            f"cannot instantiate schema in state {schema.lifecycle_state.value}; "
            "only RELEASED schemas can be instantiated"
        )
    instance = ProcessInstance(
        id=instance_id or _new_instance_id(),
        schema_id=schema.id,
        schema_version=schema.version,
        state=InstanceState.RUNNING,
        node_states={nid: NodeState.NOT_ACTIVATED for nid in schema.nodes},
        edge_states={_edge_key(e): EdgeState.NOT_SIGNALED for e in schema.edges},
        data_values=dict(initial_data or {}),
        parent_instance_id=parent_instance_id,
        parent_node_id=parent_node_id,
        is_test=is_test,
    )
    instance.node_states[schema.start_node().id] = NodeState.ACTIVATED
    if context is not None:
        context.instances.put(instance)
    _advance(instance, schema, context)
    if context is not None:
        if instance.state is InstanceState.COMPLETED:
            _trigger_follow_ups(instance, schema, context)
        context.instances.put(instance)
    return instance


def worklist(instance: ProcessInstance, schema: ProcessSchema) -> list[str]:
    """Return the ids of activities that are currently ready to be worked."""

    return [
        nid
        for nid, st in instance.node_states.items()
        if st is NodeState.ACTIVATED and schema.nodes[nid].type is NodeType.ACTIVITY
    ]


def pending_decisions(instance: ProcessInstance, schema: ProcessSchema) -> list[str]:
    """XOR splits awaiting a manual branch decision -- always empty now.

    Branch selection is fully data-driven (K7): the engine resolves every
    XOR split from its structured decision the moment it is activated, so no
    split is ever left waiting. Kept as a stable, compatibility shim.
    """

    return []


def start_activity(
    instance: ProcessInstance, schema: ProcessSchema, node_id: str
) -> ProcessInstance:
    """Move an activated ACTIVITY into the RUNNING state."""

    _require_running(instance)
    node = _require_activity(schema, node_id)
    if instance.node_states[node.id] is not NodeState.ACTIVATED:
        raise ExecutionError(
            f"activity '{node_id}' is not activated "
            f"(state {instance.node_states[node.id].value})"
        )
    result = instance.model_copy(deep=True)
    result.node_states[node.id] = NodeState.RUNNING
    return result


def complete_activity(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    data: dict[str, object] | None = None,
    *,
    agent_id: str | None = None,
    context: ExecutionContext | None = None,
) -> ProcessInstance:
    """Complete an activated/running ACTIVITY, write its data and advance.

    With a context, a completion that finishes this instance is propagated to
    the parent SUBPROCESS node that spawned it. When ``agent_id`` is given it is
    recorded as the performer of the node; if the node carries a staff rule the
    agent must be eligible for it (runtime Z enforcement), otherwise an
    ExecutionError is raised.
    """

    _require_running(instance)
    node = _require_activity(schema, node_id)
    if instance.node_states[node.id] not in (NodeState.ACTIVATED, NodeState.RUNNING):
        raise ExecutionError(
            f"activity '{node_id}' cannot be completed "
            f"(state {instance.node_states[node.id].value})"
        )
    if agent_id is not None and node_id in schema.staff_rules:
        eligible = assignment.eligible_agents(schema, node_id, instance)
        if agent_id not in eligible:
            raise ExecutionError(
                f"agent '{agent_id}' is not eligible to perform activity '{node_id}'"
            )
    result = instance.model_copy(deep=True)
    if agent_id is not None:
        result.performed_by[node.id] = agent_id
    if data:
        result.data_values.update(data)
    _complete_node(result, schema, node)
    _advance(result, schema, context)
    _finish(result, schema, context)
    return result


# --- marking propagation -------------------------------------------------


def _advance(
    instance: ProcessInstance,
    schema: ProcessSchema,
    context: ExecutionContext | None = None,
) -> None:
    """Drive the markings to a fixpoint: auto-complete gateways, then signal
    and (de)activate their targets until nothing changes."""

    progress = True
    while progress:
        progress = False
        for node in schema.nodes.values():
            if instance.node_states[node.id] is not NodeState.ACTIVATED:
                continue
            if node.type is NodeType.ACTIVITY:
                continue  # waits for interactive work
            if node.type is NodeType.SUBPROCESS and context is not None:
                if _handle_subprocess(instance, schema, node, context):
                    progress = True
                continue  # otherwise waits for its child instance
            if node.type is NodeType.END:
                instance.node_states[node.id] = NodeState.COMPLETED
                instance.state = InstanceState.COMPLETED
                progress = True
                continue
            if node.type is NodeType.XOR_SPLIT:
                target = _resolve_xor_branch(instance, schema, node)
                instance.decisions[node.id] = target
                _complete_node(instance, schema, node, chosen_target=target)
                progress = True
                continue
            _complete_node(instance, schema, node)
            progress = True
        if _evaluate_targets(instance, schema):
            progress = True


def _resolve_xor_branch(
    instance: ProcessInstance, schema: ProcessSchema, node: Node
) -> str:
    """Pick the single enabled branch of an XOR split from the instance data.

    The structured, K7-valid :class:`XorDecision` partitions the discriminator's
    domain totally and disjointly, so exactly one branch matches -- the engine
    never asks a human and can never enable two paths. A missing or ill-typed
    discriminator value is a runtime error (the modelling rules guarantee the
    value is written before the split is reached).
    """

    decision = schema.xor_decisions.get(node.id)
    if decision is None:  # pragma: no cover - guarded by K7 at release time
        raise ExecutionError(f"XOR split '{node.id}' has no branch decision")
    if decision.discriminator not in instance.data_values:
        raise ExecutionError(
            f"XOR split '{node.id}' needs data element "
            f"'{decision.discriminator}' but it is not set"
        )
    value = instance.data_values[decision.discriminator]
    target = resolve_xor_target(decision, value)
    if target is None:
        raise ExecutionError(
            f"XOR split '{node.id}' could not resolve a branch for "
            f"'{decision.discriminator}'={value!r}"
        )
    return target


# --- sub-process composition --------------------------------------------


def _finish(
    instance: ProcessInstance,
    schema: ProcessSchema,
    context: ExecutionContext | None,
) -> None:
    """Persist the instance and, if it just completed, fire its follow-ups and
    notify its parent SUBPROCESS node."""

    if context is None:
        return
    context.instances.put(instance)
    if instance.state is InstanceState.COMPLETED:
        _trigger_follow_ups(instance, schema, context)
        if instance.parent_instance_id:
            _propagate_completion(instance, context)


def _follow_up_fires(link: FollowUpLink, instance: ProcessInstance) -> bool:
    """Decide whether a follow-up link should start now.

    ON_COMPLETE links always fire on completion; CONDITIONAL links fire only if
    their predicate evaluates truthy against the completed instance's data.
    """

    if link.trigger is FollowUpTrigger.ON_COMPLETE:
        return True
    if not link.condition:
        return False
    try:
        return evaluate_condition(link.condition, instance.data_values)
    except ConditionError as exc:
        raise ExecutionError(
            f"follow-up '{link.id}' condition '{link.condition}' "
            f"could not be evaluated: {exc}"
        ) from exc


def _trigger_follow_ups(
    instance: ProcessInstance,
    schema: ProcessSchema,
    context: ExecutionContext,
) -> None:
    """Start the follow-up instances of a completed instance (F1-F3).

    A link fires when its trigger matches (ON_COMPLETE always, CONDITIONAL iff
    its predicate holds). The coupling mode decides the linkage: ASYNC starts a
    fully decoupled top-level instance (no back-reference, F3); SYNC starts a
    coupled instance that records its originating instance id for lineage. In
    both cases the new instance is seeded with the handover-mapped data and its
    id is tracked on the source.
    """

    for link in schema.follow_up_links:
        if not _follow_up_fires(link, instance):
            continue
        target = context.resolver(link.target_schema_id, link.target_version)
        if target is None:
            raise ExecutionError(
                f"follow-up target '{link.target_schema_id}' cannot be resolved"
            )
        initial_data = {
            target_elem: instance.data_values[source_elem]
            for target_elem, source_elem in link.handover_mapping.items()
            if source_elem in instance.data_values
        }
        parent_id = instance.id if link.mode is FollowUpMode.SYNC else None
        follow_up = instantiate(
            target,
            context=context,
            initial_data=initial_data,
            parent_instance_id=parent_id,
        )
        instance.follow_up_instances.append(follow_up.id)
    context.instances.put(instance)


def _handle_subprocess(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node: Node,
    context: ExecutionContext,
) -> bool:
    """Spawn the child instance for an activated SUBPROCESS node.

    The node moves to RUNNING and the parent records the child id. If the child
    completes immediately (a sub-process with no interactive step) it is joined
    in place; otherwise the parent waits for the child's later completion.
    """

    if node.id in instance.child_instances:
        return False  # already spawned, waiting for the child
    binding = schema.sub_process_bindings.get(node.id)
    if binding is None:
        _complete_node(instance, schema, node)  # no binding: opaque black box
        return True
    target = context.resolver(binding.target_schema_id, binding.target_version)
    if target is None:
        raise ExecutionError(
            f"sub-process target '{binding.target_schema_id}' "
            f"v{binding.target_version} cannot be resolved"
        )
    input_data = {
        target_elem: instance.data_values[parent_elem]
        for target_elem, parent_elem in binding.input_mapping.items()
        if parent_elem in instance.data_values
    }
    child = instantiate(
        target,
        context=context,
        parent_instance_id=instance.id,
        parent_node_id=node.id,
        initial_data=input_data,
    )
    instance.node_states[node.id] = NodeState.RUNNING
    instance.child_instances[node.id] = child.id
    if child.state is InstanceState.COMPLETED:
        _join_subprocess(instance, schema, node, child, binding)
    return True


def _join_subprocess(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node: Node,
    child: ProcessInstance,
    binding: SubProcessBinding,
) -> None:
    """Write the child's mapped outputs back and complete the SUBPROCESS node."""

    for target_elem, parent_elem in binding.output_mapping.items():
        if target_elem in child.data_values:
            instance.data_values[parent_elem] = child.data_values[target_elem]
    _complete_node(instance, schema, node)


def _propagate_completion(
    instance: ProcessInstance, context: ExecutionContext
) -> None:
    """Walk up the parent chain, joining each completed child into its parent."""

    current = instance
    while current.state is InstanceState.COMPLETED and current.parent_instance_id:
        parent = context.instances.get(current.parent_instance_id)
        if parent is None or current.parent_node_id is None:
            return
        parent_schema = context.resolver(parent.schema_id, parent.schema_version)
        if parent_schema is None:
            return
        node = parent_schema.nodes.get(current.parent_node_id)
        binding = parent_schema.sub_process_bindings.get(current.parent_node_id)
        if node is None or binding is None:
            return
        _join_subprocess(parent, parent_schema, node, current, binding)
        _advance(parent, parent_schema, context)
        context.instances.put(parent)
        if parent.state is InstanceState.COMPLETED:
            _trigger_follow_ups(parent, parent_schema, context)
        current = parent


# --- marking helpers -----------------------------------------------------


def _complete_node(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node: Node,
    chosen_target: str | None = None,
) -> None:
    """Mark a node COMPLETED and signal its outgoing edges (XOR_SPLIT signals
    exactly the chosen branch TRUE and the others FALSE)."""

    instance.node_states[node.id] = NodeState.COMPLETED
    for edge in schema.outgoing(node.id):
        if node.type is NodeType.XOR_SPLIT:
            signal = (
                EdgeState.TRUE_SIGNALED
                if edge.target == chosen_target
                else EdgeState.FALSE_SIGNALED
            )
        else:
            signal = EdgeState.TRUE_SIGNALED
        instance.edge_states[_edge_key(edge)] = signal


def _skip_node(instance: ProcessInstance, schema: ProcessSchema, node: Node) -> None:
    """Mark a node SKIPPED and propagate FALSE on all its outgoing edges."""

    instance.node_states[node.id] = NodeState.SKIPPED
    for edge in schema.outgoing(node.id):
        instance.edge_states[_edge_key(edge)] = EdgeState.FALSE_SIGNALED


def _evaluate_targets(instance: ProcessInstance, schema: ProcessSchema) -> bool:
    """Activate or skip NOT_ACTIVATED nodes whose incoming edges are resolved.

    AND_JOIN needs all incoming TRUE; any other node needs at least one TRUE.
    A node all of whose incoming edges are FALSE is skipped (propagating).
    """

    changed = False
    for node in schema.nodes.values():
        if instance.node_states[node.id] is not NodeState.NOT_ACTIVATED:
            continue
        incoming = schema.incoming(node.id)
        if not incoming:
            continue
        signals = [instance.edge_states[_edge_key(e)] for e in incoming]
        if any(s is EdgeState.NOT_SIGNALED for s in signals):
            continue  # still waiting for an upstream branch
        if node.type is NodeType.AND_JOIN:
            activate = all(s is EdgeState.TRUE_SIGNALED for s in signals)
        else:
            activate = any(s is EdgeState.TRUE_SIGNALED for s in signals)
        if activate:
            instance.node_states[node.id] = NodeState.ACTIVATED
        else:
            _skip_node(instance, schema, node)
        changed = True
    return changed


# --- guards --------------------------------------------------------------


def _require_running(instance: ProcessInstance) -> None:
    if instance.state is not InstanceState.RUNNING:
        raise ExecutionError(
            f"instance '{instance.id}' is not running (state {instance.state.value})"
        )


def _require_activity(schema: ProcessSchema, node_id: str) -> Node:
    node = schema.nodes.get(node_id)
    if node is None:
        raise ExecutionError(f"node '{node_id}' does not exist")
    if node.type is not NodeType.ACTIVITY:
        raise ExecutionError(f"node '{node_id}' is not an ACTIVITY")
    return node
