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
    NodeDetailState,
    NodeState,
    NodeType,
    ProcessInstance,
    ProcessSchema,
    SubProcessBinding,
    loop_block,
    resolve_loop_repeat,
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
#: from the instance data, K7), as is a LOOP_END (it evaluates its loop
#: decision and either exits or resets the body, K6); END is excluded because
#: it terminates the instance. A LOOP_START is a plain pass-through.
_AUTO_COMPLETE = frozenset(
    {
        NodeType.START,
        NodeType.AND_SPLIT,
        NodeType.AND_JOIN,
        NodeType.XOR_JOIN,
        NodeType.LOOP_START,
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


def claim_activity(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    agent_id: str,
    *,
    absent_agents: frozenset[str] = frozenset(),
) -> ProcessInstance:
    """Claim an offered (activated) ACTIVITY for one agent (E1, W1/W2/W4).

    The claim makes the offer exclusive: the step leaves the personal
    worklists of every other eligible agent (the "Withdrawn" view) until it is
    returned or completed. Claiming is optional -- completing an unclaimed
    step directly stays allowed -- but once claimed, only the owner can
    complete it (W2, enforced in :func:`complete_activity`).

    Guards: instance running; node is an interactive ACTIVITY in state
    ACTIVATED (no claims on automatic steps -- those belong to workers via the
    external-task lock, W4); nobody else holds the claim (W1); the agent is
    eligible per the staff rule incl. absence-gated deputies (W2). Re-claiming
    one's own claim is an idempotent no-op (double-click safe).
    """

    _require_running(instance)
    node = _require_activity(schema, node_id)
    binding = schema.service_bindings.get(node_id)
    if binding is not None and binding.automatic:
        raise ExecutionError(
            f"activity '{node_id}' is automatic and cannot be claimed (W4)"
        )
    if instance.node_states[node.id] is not NodeState.ACTIVATED:
        raise ExecutionError(
            f"activity '{node_id}' is not activated "
            f"(state {instance.node_states[node.id].value})"
        )
    holder = instance.claimed_by.get(node.id)
    if holder == agent_id:
        return instance.model_copy(deep=True)
    if holder is not None:
        raise ExecutionError(
            f"activity '{node_id}' is already claimed by '{holder}' (W1)"
        )
    if node_id in schema.staff_rules:
        eligible = assignment.eligible_agents(
            schema, node_id, instance, absent_agents=absent_agents
        )
        if agent_id not in eligible:
            raise ExecutionError(
                f"agent '{agent_id}' is not eligible to claim activity "
                f"'{node_id}' (W2)"
            )
    result = instance.model_copy(deep=True)
    result.claimed_by[node.id] = agent_id
    return result


def return_activity(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    agent_id: str,
    *,
    force: bool = False,
) -> ProcessInstance:
    """Return a claimed ACTIVITY to the open offer (E1, W3).

    The claim is cleared and a RUNNING step falls back to ACTIVATED, so the
    step reappears in the personal worklists of every eligible agent. Only the
    owner may return; ``force`` is the boundary's supervisor/admin override
    (the *authority* decision lives at the API, the engine only distinguishes
    owner vs. override).
    """

    _require_running(instance)
    node = _require_activity(schema, node_id)
    if instance.node_details.get(node_id) is NodeDetailState.FAILED:
        # E2 (V4): a failed step is recovered via reset (fresh offer +
        # re-stamped clock), never silently returned.
        raise ExecutionError(
            f"activity '{node_id}' is marked FAILED -- use its reset instead (V4)"
        )
    holder = instance.claimed_by.get(node.id)
    if holder is None:
        raise ExecutionError(f"activity '{node_id}' is not claimed")
    if not force and holder != agent_id:
        raise ExecutionError(
            f"activity '{node_id}' is claimed by '{holder}', not '{agent_id}' (W3)"
        )
    result = instance.model_copy(deep=True)
    result.claimed_by.pop(node.id, None)
    result.node_claimed_at.pop(node.id, None)
    result.node_details.pop(node.id, None)  # returning ends a pause (E2)
    result.node_paused_seconds.pop(node.id, None)  # net-time credit too
    result.node_suspended_at.pop(node.id, None)
    if result.node_states[node.id] is NodeState.RUNNING:
        result.node_states[node.id] = NodeState.ACTIVATED
    return result


def start_activity(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    agent_id: str,
    *,
    absent_agents: frozenset[str] = frozenset(),
) -> ProcessInstance:
    """Move an activated ACTIVITY into the RUNNING state (E1: Started).

    Starting presupposes ownership (W4): an unclaimed step is claimed
    implicitly for ``agent_id`` (Offered -> Started collapses Allocated), a
    step claimed by someone else is refused. The RUNNING marking is what the
    §6.2.1 state machine calls *Started*.
    """

    claimed = claim_activity(
        instance, schema, node_id, agent_id, absent_agents=absent_agents
    )
    claimed.node_states[node_id] = NodeState.RUNNING
    return claimed


def _require_started_owner(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    agent_id: str,
    absent_agents: frozenset[str],
) -> ProcessInstance:
    """Collapse to the Started state under ``agent_id`` (V1/V3 helper).

    Suspend/fail act on a *started, owned* step; a merely offered or claimed
    step is started implicitly (the same §6.2.1 collapse ``start_activity``
    performs), while a step owned by someone else is refused there (W1).
    A FAILED step is frozen until its recovery (V4) and refuses everything.
    """

    if instance.node_details.get(node_id) is NodeDetailState.FAILED:
        raise ExecutionError(
            f"activity '{node_id}' is marked FAILED and awaits its reset (V4)"
        )
    if instance.node_states.get(node_id) is NodeState.RUNNING:
        holder = instance.claimed_by.get(node_id)
        if holder != agent_id:
            raise ExecutionError(
                f"activity '{node_id}' is worked on by '{holder}', not "
                f"'{agent_id}' (V1)"
            )
        return instance.model_copy(deep=True)
    return start_activity(
        instance, schema, node_id, agent_id, absent_agents=absent_agents
    )


def suspend_activity(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    agent_id: str,
    *,
    absent_agents: frozenset[str] = frozenset(),
) -> ProcessInstance:
    """Pause a started ACTIVITY: RUNNING -> SUSPENDED overlay (E2, V1).

    Owner-only; idempotent for a re-suspend by the owner. The base marking
    stays RUNNING (nothing propagates), the overlay blocks completion until
    ``resume_activity`` (V2) -- the §4 automaton only finishes from RUNNING.
    By default the clock deliberately keeps running (a pause is transparency,
    not a deadline stop -- otherwise suspending would dodge the escalation);
    only a constraint with the explicit ``pause_stops_clock`` opt-in earns
    net-time credit, booked at the API boundary (the engine stays clock-free).
    """

    _require_running(instance)
    _require_activity(schema, node_id)
    if instance.node_details.get(node_id) is NodeDetailState.SUSPENDED:
        if instance.claimed_by.get(node_id) != agent_id:
            raise ExecutionError(
                f"activity '{node_id}' is suspended by its owner, not '{agent_id}'"
            )
        return instance.model_copy(deep=True)
    result = _require_started_owner(instance, schema, node_id, agent_id, absent_agents)
    result.node_details[node_id] = NodeDetailState.SUSPENDED
    return result


def resume_activity(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    agent_id: str,
) -> ProcessInstance:
    """Continue a suspended ACTIVITY: SUSPENDED -> RUNNING (E2, V2)."""

    _require_running(instance)
    _require_activity(schema, node_id)
    if instance.node_details.get(node_id) is not NodeDetailState.SUSPENDED:
        raise ExecutionError(f"activity '{node_id}' is not suspended")
    if instance.claimed_by.get(node_id) != agent_id:
        raise ExecutionError(
            f"activity '{node_id}' can only be resumed by its owner (V2)"
        )
    result = instance.model_copy(deep=True)
    result.node_details.pop(node_id, None)
    return result


def fail_activity(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    agent_id: str,
    reason: str,
    *,
    absent_agents: frozenset[str] = frozenset(),
) -> ProcessInstance:
    """Mark a started ACTIVITY as failed: RUNNING -> FAILED overlay (E2, V3).

    Owner-only (implicit start collapse like V1); a suspended step resumes
    first (the §4 automaton aborts only from RUNNING). The failed step is
    frozen -- not completable, not workable -- until its recovery
    (``reset_activity``, V4): the instance waits *defined*, never undefined.
    """

    _require_running(instance)
    _require_activity(schema, node_id)
    if instance.node_details.get(node_id) is NodeDetailState.SUSPENDED:
        raise ExecutionError(
            f"activity '{node_id}' is suspended -- resume before failing (V3)"
        )
    result = _require_started_owner(instance, schema, node_id, agent_id, absent_agents)
    result.node_details[node_id] = NodeDetailState.FAILED
    result.node_detail_reason[node_id] = reason.strip()
    return result


def reset_activity(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    agent_id: str,
    *,
    force: bool = False,
) -> ProcessInstance:
    """Recover a failed ACTIVITY: FAILED -> ACTIVATED, fresh offer (E2, V4).

    Owner may always reset their own failure; ``force`` is the boundary's
    supervisory override (like the E1 return). The reset clears overlay,
    reason, claim and fired escalation stages, and puts the base marking back
    to ACTIVATED -- the task becomes a fresh offer to every eligible agent;
    the boundary re-stamps the activation clock (fresh deadline and a fresh
    escalation ladder for the second attempt).
    """

    _require_running(instance)
    _require_activity(schema, node_id)
    if instance.node_details.get(node_id) is not NodeDetailState.FAILED:
        raise ExecutionError(f"activity '{node_id}' is not marked FAILED")
    holder = instance.claimed_by.get(node_id)
    if not force and holder != agent_id:
        raise ExecutionError(
            f"activity '{node_id}' failed under '{holder}'; only they or a "
            "supervisor may reset it (V4)"
        )
    result = instance.model_copy(deep=True)
    result.node_details.pop(node_id, None)
    result.node_detail_reason.pop(node_id, None)
    result.claimed_by.pop(node_id, None)
    result.node_claimed_at.pop(node_id, None)
    result.escalated_stages.pop(node_id, None)
    result.node_paused_seconds.pop(node_id, None)  # fresh clocks (net time)
    result.node_suspended_at.pop(node_id, None)
    result.node_states[node_id] = NodeState.ACTIVATED
    return result


def complete_activity(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node_id: str,
    data: dict[str, object] | None = None,
    *,
    agent_id: str | None = None,
    context: ExecutionContext | None = None,
    absent_agents: frozenset[str] = frozenset(),
) -> ProcessInstance:
    """Complete an activated/running ACTIVITY, write its data and advance.

    With a context, a completion that finishes this instance is propagated to
    the parent SUBPROCESS node that spawned it. When ``agent_id`` is given it is
    recorded as the performer of the node; if the node carries a staff rule the
    agent must be eligible for it (runtime Z enforcement), otherwise an
    ExecutionError is raised. ``absent_agents`` is the currently-absent set used
    to admit a deputy standing in for an absent agent (see
    :func:`procworks.assignment.eligible_agents`); empty by default.
    """

    _require_running(instance)
    node = _require_activity(schema, node_id)
    if instance.node_states[node.id] not in (NodeState.ACTIVATED, NodeState.RUNNING):
        raise ExecutionError(
            f"activity '{node_id}' cannot be completed "
            f"(state {instance.node_states[node.id].value})"
        )
    if agent_id is not None and node_id in schema.staff_rules:
        eligible = assignment.eligible_agents(
            schema, node_id, instance, absent_agents=absent_agents
        )
        if agent_id not in eligible:
            raise ExecutionError(
                f"agent '{agent_id}' is not eligible to perform activity '{node_id}'"
            )
    holder = instance.claimed_by.get(node.id)
    if holder is not None and holder != agent_id:
        # W2 (E1): a claim is binding -- once someone has taken the step over,
        # nobody else (including an anonymous caller) may complete it.
        raise ExecutionError(
            f"activity '{node_id}' is claimed by '{holder}' and can only be "
            "completed by them (W2)"
        )
    detail = instance.node_details.get(node.id)
    if detail is NodeDetailState.SUSPENDED:
        # E2 (V2): the §4 automaton only finishes from RUNNING -- resume first.
        raise ExecutionError(
            f"activity '{node_id}' is suspended -- resume it before completing"
        )
    if detail is NodeDetailState.FAILED:
        raise ExecutionError(
            f"activity '{node_id}' is marked FAILED and awaits its reset (V4)"
        )
    result = instance.model_copy(deep=True)
    if agent_id is not None:
        result.performed_by[node.id] = agent_id
    result.claimed_by.pop(node.id, None)
    result.node_claimed_at.pop(node.id, None)
    result.node_paused_seconds.pop(node.id, None)  # net-time credit is per activation
    result.node_suspended_at.pop(node.id, None)
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
            if node.type is NodeType.LOOP_END:
                _resolve_loop_end(instance, schema, node)
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


def _resolve_loop_end(
    instance: ProcessInstance, schema: ProcessSchema, node: Node
) -> None:
    """Decide a REPEAT-UNTIL loop at its LOOP_END (K6, Schleifen-Konzept §6).

    Evaluates the structured ``LoopDecision`` against the instance data via
    :func:`procworks.model.resolve_loop_repeat` (boolean shorthand or S3
    repeat/exit partition). On repeat the markings of the whole block
    (LOOP_START, body, LOOP_END) are reset to NOT_ACTIVATED and every
    block-internal edge to NOT_SIGNALED, while the loop start's *incoming*
    edge stays signalled, so the standard fixpoint re-activates the start and
    the body runs again. Otherwise the LOOP_END completes normally and the
    flow leaves the block.

    K6c guarantees the discriminator is freshly written on every path through
    the body, so the decision is defined in every iteration; a missing or
    (for a partition) ill-typed value can only mean the model bypassed
    validation and is a runtime error.
    """

    decision = schema.loop_decisions.get(node.id)
    if decision is None:  # pragma: no cover - guarded by K6b at commit time
        raise ExecutionError(f"LOOP_END '{node.id}' has no loop decision")
    if decision.discriminator not in instance.data_values:
        raise ExecutionError(
            f"LOOP_END '{node.id}' needs data element "
            f"'{decision.discriminator}' but it is not set"
        )
    value = instance.data_values[decision.discriminator]
    repeat = resolve_loop_repeat(decision, value)
    if repeat is None:
        raise ExecutionError(
            f"LOOP_END '{node.id}' could not classify "
            f"'{decision.discriminator}'={value!r} into repeat or exit"
        )
    if repeat and decision.max_iterations is not None:
        # Deterministic hard brake (S3): the body has already run
        # (iterations + 1) times; another repeat would make it iterations + 2.
        # At the bound the loop exits even though the data says repeat.
        if instance.loop_iterations.get(node.id, 0) + 2 > decision.max_iterations:
            repeat = False
    if repeat:
        start_id = next(
            nid
            for nid, n in schema.nodes.items()
            if n.type is NodeType.LOOP_START
            and loop_block(schema, nid)[0] == node.id
        )
        _, body = loop_block(schema, start_id)
        _reset_loop_block(instance, schema, start_id, node.id, body)
        instance.loop_iterations[node.id] = (
            instance.loop_iterations.get(node.id, 0) + 1
        )
        return
    _complete_node(instance, schema, node)


def _reset_loop_block(
    instance: ProcessInstance,
    schema: ProcessSchema,
    start_id: str,
    end_id: str,
    body: set[str],
) -> None:
    """Reset a loop block's markings for the next iteration.

    All block nodes go back to NOT_ACTIVATED and all block-internal edges
    (those leaving the start or a body node) to NOT_SIGNALED. The start's
    incoming edge keeps its TRUE signal from before the loop, which is exactly
    what re-activates the start in the next ``_evaluate_targets`` pass; the
    end's outgoing edge is untouched (still NOT_SIGNALED -- the loop has not
    been left). Data values persist across iterations by design (the body
    overwrites what it re-writes, K6c guarantees the discriminator among it).

    A body SUBPROCESS node also sheds its child-instance link: the block
    structure guarantees the child of a completed iteration is COMPLETED
    (otherwise its node -- and thus the LOOP_END -- could never have been
    reached), so dropping the link is what lets ``_handle_subprocess`` spawn a
    *fresh* child in the next iteration instead of waiting forever on the old
    one (stage S3 -- the reason the former K6e restriction could be lifted).
    ``child_instances`` therefore always maps a node to its *latest* child;
    earlier iterations' children remain in the store and keep their own
    ``parent_instance_id``/``parent_node_id`` back-references.
    """

    block = body | {start_id, end_id}
    for nid in block:
        instance.node_states[nid] = NodeState.NOT_ACTIVATED
        instance.child_instances.pop(nid, None)
        # W4 (E1): every iteration is a fresh offer -- a claim never survives
        # the activation it was made for.
        instance.claimed_by.pop(nid, None)
        instance.node_claimed_at.pop(nid, None)
        # T3/E9: each round measures its own target time -- fired escalation
        # stages belong to the activation, not the node.
        instance.escalated_stages.pop(nid, None)
        # E2: detail overlays hang on one activation like claims do.
        instance.node_details.pop(nid, None)
        instance.node_detail_reason.pop(nid, None)
        instance.node_paused_seconds.pop(nid, None)  # net-time credit too
        instance.node_suspended_at.pop(nid, None)
    internal_sources = body | {start_id}
    for edge in schema.edges:
        if edge.source in internal_sources:
            instance.edge_states[_edge_key(edge)] = EdgeState.NOT_SIGNALED


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
    """Walk up the parent chain, joining each completed child into its parent.

    Only the child the parent *currently* waits for may join: with loops a
    SUBPROCESS node can spawn one child per iteration, and the loop reset
    re-keys ``child_instances`` to the latest child. A child from an earlier
    iteration whose link was dropped must never complete the (re-activated)
    parent node of a later iteration, so a stale child stops the walk.
    """

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
        if parent.child_instances.get(current.parent_node_id) != current.id:
            return  # stale child of an earlier loop iteration
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
    for edge in schema.sync_outgoing(node.id):
        # K4: a sync edge is *resolved* by completion -- its waiter may run.
        instance.edge_states[_edge_key(edge)] = EdgeState.TRUE_SIGNALED


def _skip_node(instance: ProcessInstance, schema: ProcessSchema, node: Node) -> None:
    """Mark a node SKIPPED and propagate FALSE on all its outgoing edges."""

    instance.node_states[node.id] = NodeState.SKIPPED
    for edge in schema.outgoing(node.id):
        instance.edge_states[_edge_key(edge)] = EdgeState.FALSE_SIGNALED
    for edge in schema.sync_outgoing(node.id):
        # K4: deselection resolves the wait too (never a dead-wait) -- the
        # FALSE signal carries "resolved without execution".
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
        if activate and any(
            instance.edge_states.get(_edge_key(e)) is EdgeState.NOT_SIGNALED
            for e in schema.sync_incoming(node.id)
        ):
            # K4: an incoming sync edge is an *additional* wait -- the node
            # activates only once every sync source is completed or skipped.
            # A node the control flow deselects skips regardless (a skip must
            # never dead-wait on a sync).
            continue
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
