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

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from procworks import assignment, ids
from procworks.conditions import ConditionError, evaluate_condition
from procworks.model import (
    WRITE_MODES,
    ControlEdge,
    DecisionRecord,
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


@dataclass
class ExecutionContext:
    """Wiring the engine needs to run composed (sub-/follow-up) processes.

    ``resolver`` resolves a pinned target schema (composition rules H1-H4) and
    ``instances`` persists the spawned child instances. When no context is
    given the engine treats a SUBPROCESS node as an opaque black box that
    completes immediately (the step 8 behaviour).

    ``on_spawn`` is an optional, purely observing callback the engine invokes
    for every instance it starts *on its own* -- a sub-process child or a
    follow-up instance -- as ``on_spawn(spawned, origin)`` once ``spawned`` has
    been instantiated and advanced (it may already be COMPLETED); ``origin`` is
    the instance whose progress started it. Such instances never pass through
    a start request, so this is the only point where the boundary learns about
    them (it records their creation in the audit and gives them the start
    side effects of a direct start -- activation stamps, mail, the
    ``instance.started`` event). The callback must not change the markings or
    data of either instance and must not raise; it may set boundary-only
    fields of ``spawned`` (activation stamps) and store it again. The engine
    does not depend on it, and ``None`` (the default) keeps the engine free of
    any side effect.
    """

    resolver: SchemaResolver
    instances: InstanceStore
    on_spawn: Callable[[ProcessInstance, ProcessInstance], None] | None = None

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
    """Raised when a runtime operation is not allowed in the current state.

    ``message`` stays the technical English base (tests, API users, logs).
    ``code``/``params`` let a client word it -- the web client formulates it in
    its one catalogue ``FINDING_TEXTS`` like any rule finding. With the message
    alone, a colleague who claimed the same task a second earlier would be
    shown "activity 'act_1' is already claimed by 'a-tom' (W1)". ``params`` name
    steps by their label (``step``) and agents by id (``agent``), never only
    by node id: the client may be showing another schema than the instance's.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "EX.not-allowed",
        params: dict[str, str] | None = None,
    ) -> None:
        self.message = message
        self.code = code
        self.params = params or {}
        super().__init__(message)


def _step(schema: ProcessSchema, node_id: str) -> str:
    """Readable name of a step for an error's ``params`` (label, else its id)."""

    node = schema.nodes.get(node_id)
    return node.label if node is not None and node.label else node_id


def _not_eligible_params(
    schema: ProcessSchema, node_id: str, instance: ProcessInstance, agent_id: str, action: str
) -> dict[str, str]:
    """``params`` of an ``EX.not-eligible`` refusal, with the reason when it is four eyes.

    Always ``step`` (the refused step's name). When the agent is kept out by
    the four-eyes part of the step's rule -- they did an earlier step the rule
    excludes or has a supervisor approve -- also ``reason`` ``"four-eyes"``
    and ``other_step`` (that earlier step's name): someone who holds the role
    would otherwise read only "not responsible" and not understand why.
    With the four-eyes reason also ``action`` -- ``"claim"`` or
    ``"complete"`` -- so the message names the refused act (a refused
    completion is no refused "taking over").

    :param schema: the effective schema of the instance.
    :param node_id: the refused step.
    :param instance: the case before the refused act.
    :param agent_id: the refused agent.
    :param action: the refused act, ``"claim"`` or ``"complete"``.
    :returns: the ``params`` dict (strings only, like every engine refusal);
        without the four-eyes reason only ``step`` (that text names no act).
    """

    params = {"step": _step(schema, node_id)}
    other = assignment.four_eyes_reference(schema, node_id, instance, agent_id)
    if other is not None:
        params["reason"] = "four-eyes"
        params["other_step"] = _step(schema, other)
        params["action"] = action
    return params


def _element(schema: ProcessSchema, element_id: str) -> str:
    """Readable name of a data element for an error's ``params``."""

    element = schema.data_elements.get(element_id)
    return element.name if element is not None and element.name else element_id


def _new_instance_id() -> str:
    """Return a fresh ``instance_<n>`` id from the shared, store-guarded sequence.

    Never a process-local counter again: that restarted at 1 after every API
    restart and would overwrite stored instances (see :mod:`procworks.ids`).
    """

    return ids.INSTANCE_IDS.new("instance")


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
            "only RELEASED schemas can be instantiated",
            code="EX.not-released",
            params={},
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
            f"activity '{node_id}' is automatic and cannot be claimed (W4)",
            code="EX.automatic",
            params={"step": _step(schema, node_id)},
        )
    if instance.node_states[node.id] is not NodeState.ACTIVATED:
        raise ExecutionError(
            f"activity '{node_id}' is not activated "
            f"(state {instance.node_states[node.id].value})",
            code="EX.not-ready",
            params={"step": _step(schema, node_id)},
        )
    holder = instance.claimed_by.get(node.id)
    if holder == agent_id:
        return instance.model_copy(deep=True)
    if holder is not None:
        raise ExecutionError(
            f"activity '{node_id}' is already claimed by '{holder}' (W1)",
            code="EX.claimed-by-other",
            params={"step": _step(schema, node_id), "agent": str(holder)},
        )
    if node_id in schema.staff_rules:
        eligible = assignment.eligible_agents(
            schema, node_id, instance, absent_agents=absent_agents
        )
        if agent_id not in eligible:
            raise ExecutionError(
                f"agent '{agent_id}' is not eligible to claim activity "
                f"'{node_id}' (W2)",
                code="EX.not-eligible",
                params=_not_eligible_params(schema, node_id, instance, agent_id, "claim"),
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
            f"activity '{node_id}' is marked FAILED -- use its reset instead (V4)",
            code="EX.failed",
            params={"step": _step(schema, node_id)},
        )
    holder = instance.claimed_by.get(node.id)
    if holder is None:
        raise ExecutionError(
            f"activity '{node_id}' is not claimed",
            code="EX.not-claimed",
            params={"step": _step(schema, node_id)},
        )
    if not force and holder != agent_id:
        raise ExecutionError(
            f"activity '{node_id}' is claimed by '{holder}', not '{agent_id}' (W3)",
            code="EX.claimed-by-other",
            params={"step": _step(schema, node_id), "agent": str(holder)},
        )
    result = instance.model_copy(deep=True)
    _release_claim(result, node.id)
    result.node_details.pop(node.id, None)  # returning ends a pause (E2)
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
    worklist state machine calls *Started*.
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
    step is started implicitly (the same collapse ``start_activity``
    performs), while a step owned by someone else is refused there (W1).
    A FAILED step is frozen until its recovery (V4) and refuses everything.
    """

    if instance.node_details.get(node_id) is NodeDetailState.FAILED:
        raise ExecutionError(
            f"activity '{node_id}' is marked FAILED and awaits its reset (V4)",
            code="EX.failed",
            params={"step": _step(schema, node_id)},
        )
    if instance.node_states.get(node_id) is NodeState.RUNNING:
        holder = instance.claimed_by.get(node_id)
        if holder != agent_id:
            raise ExecutionError(
                f"activity '{node_id}' is worked on by '{holder}', not "
                f"'{agent_id}' (V1)",
                code="EX.claimed-by-other",
                params={"step": _step(schema, node_id), "agent": str(holder)},
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
    ``resume_activity`` (V2) -- the detail-state automaton only finishes from RUNNING.
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
                f"activity '{node_id}' is suspended by its owner, not '{agent_id}'",
                code="EX.suspended-by-other",
                params={"step": _step(schema, node_id)},
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
        raise ExecutionError(
            f"activity '{node_id}' is not suspended",
            code="EX.not-suspended",
            params={"step": _step(schema, node_id)},
        )
    if instance.claimed_by.get(node_id) != agent_id:
        raise ExecutionError(
            f"activity '{node_id}' can only be resumed by its owner (V2)",
            code="EX.owner-only",
            params={"step": _step(schema, node_id)},
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
    first (the detail-state automaton aborts only from RUNNING). The failed step is
    frozen -- not completable, not workable -- until its recovery
    (``reset_activity``, V4): the instance waits *defined*, never undefined.
    """

    _require_running(instance)
    _require_activity(schema, node_id)
    if instance.node_details.get(node_id) is NodeDetailState.SUSPENDED:
        raise ExecutionError(
            f"activity '{node_id}' is suspended -- resume before failing (V3)",
            code="EX.suspended",
            params={"step": _step(schema, node_id)},
        )
    result = _require_started_owner(instance, schema, node_id, agent_id, absent_agents)
    result.node_details[node_id] = NodeDetailState.FAILED
    result.node_detail_reason[node_id] = reason.strip()
    return result


def record_data_correction(
    instance: ProcessInstance, schema: ProcessSchema, element_ids: Iterable[str]
) -> ProcessInstance:
    """Note a data correction (``PUT …/data``) for the open steps that write it.

    Once a loop repeats a step (the step itself completed before,
    :func:`procworks.validator.is_repeated_activation`), a mandatory write of
    the step counts as supplied only when the completion brings the value or
    a correction set it during the step's *current* activation
    (:func:`procworks.validator.completion_value_findings`; in the step's
    first activation every stored value counts). This records the second
    case: each corrected element is added to ``data_corrected_in_activation``
    of every node that is ACTIVATED or RUNNING **and writes it**
    (``WRITE``/``READ_WRITE`` access in ``schema``). An element the step does
    not write is no value the step supplies, so it is not listed there. The
    values themselves are not touched; the caller stores them.

    A correction made while the step is **not open yet** (NOT_ACTIVATED --
    say, its predecessor in the loop body still runs) is recorded for no
    activation of it: in a repetition it does not supply the step, whose
    activation starts only when it becomes ready ("set in this round after
    the step's activation"). In the step's first activation it needs no
    record -- every stored value counts there.

    :param instance: the case before the correction (not changed).
    :param schema: the schema the case runs against (its ad-hoc variant, if
        any) -- it tells which step writes which element.
    :param element_ids: the elements the correction sets -- also those whose
        value stays the same: confirming a value is setting it in this round.
    :returns: a copy with the record extended (entries sorted, unique). A
        case without an open step writing one of the elements comes back with
        an unchanged record: the correction then counts for no step's
        activation.
    """

    result = instance.model_copy(deep=True)
    ids_set = set(element_ids)
    if not ids_set:
        return result
    for node_id, state in result.node_states.items():
        if state not in (NodeState.ACTIVATED, NodeState.RUNNING):
            continue
        written = ids_set & {
            access.element_id
            for access in schema.accesses_of(node_id)
            if access.mode in WRITE_MODES
        }
        if not written:
            continue
        known = set(result.data_corrected_in_activation.get(node_id, ()))
        result.data_corrected_in_activation[node_id] = sorted(known | written)
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
        raise ExecutionError(
            f"activity '{node_id}' is not marked FAILED",
            code="EX.not-failed",
            params={"step": _step(schema, node_id)},
        )
    holder = instance.claimed_by.get(node_id)
    if not force and holder != agent_id:
        raise ExecutionError(
            f"activity '{node_id}' failed under '{holder}'; only they or a "
            "supervisor may reset it (V4)",
            code="EX.owner-only",
            params={"step": _step(schema, node_id)},
        )
    result = instance.model_copy(deep=True)
    _clear_activation(result, node_id)
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
            f"(state {instance.node_states[node.id].value})",
            code="EX.not-ready",
            params={"step": _step(schema, node_id)},
        )
    if agent_id is not None and node_id in schema.staff_rules:
        eligible = assignment.eligible_agents(
            schema, node_id, instance, absent_agents=absent_agents
        )
        if agent_id not in eligible:
            raise ExecutionError(
                f"agent '{agent_id}' is not eligible to perform activity '{node_id}'",
                code="EX.not-eligible",
                params=_not_eligible_params(schema, node_id, instance, agent_id, "complete"),
            )
    holder = instance.claimed_by.get(node.id)
    if holder is not None and holder != agent_id:
        # W2 (E1): a claim is binding -- once someone has taken the step over,
        # nobody else (including an anonymous caller) may complete it.
        raise ExecutionError(
            f"activity '{node_id}' is claimed by '{holder}' and can only be "
            "completed by them (W2)",
            code="EX.claimed-by-other",
            params={"step": _step(schema, node_id), "agent": str(holder)},
        )
    detail = instance.node_details.get(node.id)
    if detail is NodeDetailState.SUSPENDED:
        # E2 (V2): the detail-state automaton only finishes from RUNNING -- resume first.
        raise ExecutionError(
            f"activity '{node_id}' is suspended -- resume it before completing",
            code="EX.suspended",
            params={"step": _step(schema, node_id)},
        )
    if detail is NodeDetailState.FAILED:
        raise ExecutionError(
            f"activity '{node_id}' is marked FAILED and awaits its reset (V4)",
            code="EX.failed",
            params={"step": _step(schema, node_id)},
        )
    result = instance.model_copy(deep=True)
    if agent_id is not None:
        result.performed_by[node.id] = agent_id
    _release_claim(result, node.id)
    _end_corrections(result, node.id)
    # The step has run once more: from now on each further activation of it
    # (a later loop round) is a repetition (validator.is_repeated_activation).
    result.node_completions[node.id] = result.node_completions.get(node.id, 0) + 1
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
    and (de)activate their targets until nothing changes.

    Loop safety net: within **one** advance a LOOP_END may say "repeat" at most
    once. On a K6-valid model a second repeat is impossible -- K6c puts a
    writing ACTIVITY on every path through the body, and an activity always
    waits for work, so after a reset the fixpoint stops inside the body. If it
    happens anyway, the model is not block-structured (a loop straddling a
    branch block, stored before K6a checked that on 2026-09-24): the reset
    re-skips the body and the same LOOP_END repeats forever -- a request that
    never returns. Raising :class:`ExecutionError` instead turns the hang into
    a 409; on the usual path -- completing the step before the loop end --
    :func:`complete_activity` works on a copy, so the instance stays where it
    was.
    """

    repeated: set[str] = set()
    progress = True
    while progress:
        progress = False
        for node in schema.nodes.values():
            if instance.node_states[node.id] is not NodeState.ACTIVATED:
                continue
            if _fire_activated_node(instance, schema, node, context, repeated):
                progress = True
        if _evaluate_targets(instance, schema):
            progress = True


def _fire_activated_node(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node: Node,
    context: ExecutionContext | None,
    repeated: set[str],
) -> bool:
    """Let one ACTIVATED node do what its type does automatically.

    One step of the :func:`_advance` fixpoint, dispatched on the node type:

    * ``ACTIVITY`` -- nothing; it waits for interactive work.
    * ``SUBPROCESS`` -- with a ``context``, :func:`_handle_subprocess` starts
      or finishes the child instance (and otherwise the node waits for it);
      without a context it is completed like a plain node (last case below).
    * ``END`` -- completes the node and the whole instance.
    * ``XOR_SPLIT`` -- picks the branch from the instance data, records the
      decision (``decisions`` and, for the audit, ``unaudited_decisions``) and
      completes the split towards that branch only.
    * ``LOOP_END`` -- repeats or exits the loop (:func:`_fire_loop_end`).
    * any other node (START, LOOP_START, AND split, joins, and a SUBPROCESS
      without context) -- completes and signals all its outgoing edges.

    :param instance: the instance whose markings are advanced (mutated)
    :param schema: the released schema the instance runs on
    :param node: an ACTIVATED node of ``schema``
    :param context: execution context for sub-process handling, or ``None``
    :param repeated: LOOP_END ids that already repeated in this advance;
        updated in place for the loop safety net
    :returns: ``True`` when the markings changed (the fixpoint has to run
        another round), ``False`` when the node keeps waiting
    :raises ExecutionError: from the XOR/loop resolution, or when a LOOP_END
        repeats a second time within the same advance
    """

    if node.type is NodeType.ACTIVITY:
        return False  # waits for interactive work
    if node.type is NodeType.SUBPROCESS and context is not None:
        # False: the node waits for its child instance.
        return _handle_subprocess(instance, schema, node, context)
    if node.type is NodeType.END:
        instance.node_states[node.id] = NodeState.COMPLETED
        instance.state = InstanceState.COMPLETED
        return True
    if node.type is NodeType.XOR_SPLIT:
        target = _resolve_xor_branch(instance, schema, node)
        instance.decisions[node.id] = target
        # Hand the decision over to the boundary's audit (BRANCH_DECIDED);
        # the engine itself stays free of side effects.
        decision = schema.xor_decisions[node.id]
        instance.unaudited_decisions.append(
            DecisionRecord(
                node_id=node.id,
                target=target,
                value=instance.data_values.get(decision.discriminator),
            )
        )
        _complete_node(instance, schema, node, chosen_target=target)
        return True
    if node.type is NodeType.LOOP_END:
        _fire_loop_end(instance, schema, node, repeated)
        return True
    _complete_node(instance, schema, node)
    return True


def _fire_loop_end(
    instance: ProcessInstance,
    schema: ProcessSchema,
    node: Node,
    repeated: set[str],
) -> None:
    """Decide an ACTIVATED LOOP_END and enforce the one-repeat safety net.

    Delegates the decision to :func:`_resolve_loop_end` (which resets the block
    on repeat or completes the node on exit). A repeat is recorded in
    ``repeated``; a second repeat of the same LOOP_END within one
    :func:`_advance` means no work happened in between -- see the safety-net
    note on :func:`_advance` for why that only occurs on a model that is not
    properly nested.

    :param instance: the instance whose markings are advanced (mutated)
    :param schema: the released schema the instance runs on
    :param node: the ACTIVATED LOOP_END node
    :param repeated: LOOP_END ids that already repeated in this advance;
        ``node.id`` is added on a repeat
    :raises ExecutionError: ``EX.loop-broken`` on the second repeat, plus
        whatever :func:`_resolve_loop_end` raises
    """

    if not _resolve_loop_end(instance, schema, node):
        return
    if node.id in repeated:
        raise ExecutionError(
            f"LOOP_END '{node.id}' repeated without any work in "
            "between -- the loop is not properly nested with a "
            "branch block (K6a); the model must be corrected",
            code="EX.loop-broken",
            params={},
        )
    repeated.add(node.id)


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
        raise ExecutionError(
            f"XOR split '{node.id}' has no branch decision",
            code="EX.no-decision",
            params={},
        )
    value = _discriminator_value(
        instance, schema, f"XOR split '{node.id}'", decision.discriminator
    )
    target = resolve_xor_target(decision, value)
    if target is None:
        raise ExecutionError(
            f"XOR split '{node.id}' could not resolve a branch for "
            f"'{decision.discriminator}'={value!r}",
            code="EX.no-branch",
            params={"element": _element(schema, decision.discriminator)},
        )
    return target


def _resolve_loop_end(
    instance: ProcessInstance, schema: ProcessSchema, node: Node
) -> bool:
    """Decide a REPEAT-UNTIL loop at its LOOP_END (K6).

    Returns ``True`` when the loop repeats (block reset), ``False`` when it
    exits -- :func:`_advance` uses that for its loop safety net.

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
        raise ExecutionError(
            f"LOOP_END '{node.id}' has no loop decision",
            code="EX.no-decision",
            params={},
        )
    value = _discriminator_value(
        instance, schema, f"LOOP_END '{node.id}'", decision.discriminator
    )
    repeat = resolve_loop_repeat(decision, value)
    if repeat is None:
        raise ExecutionError(
            f"LOOP_END '{node.id}' could not classify "
            f"'{decision.discriminator}'={value!r} into repeat or exit",
            code="EX.no-branch",
            params={"element": _element(schema, decision.discriminator)},
        )
    if repeat and decision.max_iterations is not None:
        # Deterministic hard brake (S3): the body has already run
        # (iterations + 1) times; another repeat would make it iterations + 2.
        # At the bound the loop exits even though the data says repeat.
        if instance.loop_iterations.get(node.id, 0) + 2 > decision.max_iterations:
            repeat = False
    if repeat:
        start_id, body = _loop_of_end(schema, node.id)
        _reset_loop_block(instance, schema, start_id, node.id, body)
        instance.loop_iterations[node.id] = (
            instance.loop_iterations.get(node.id, 0) + 1
        )
        return True
    _complete_node(instance, schema, node)
    return False


def _discriminator_value(
    instance: ProcessInstance,
    schema: ProcessSchema,
    subject: str,
    discriminator: str,
) -> object:
    """Read the discriminator value a structured decision is evaluated on.

    Shared by the XOR split (K7) and the LOOP_END (K6) resolution: both need
    the value of their decision's discriminator element from the instance data.

    ``subject`` names the deciding node for the technical message (e.g.
    ``"XOR split 'x_1'"`` or ``"LOOP_END 'le_1'"``); ``discriminator`` is the
    data element id. Returns the stored value as is -- classifying it (branch,
    repeat/exit) stays with the caller, which also words its own refusal for a
    value that fits no branch.

    Raises :class:`ExecutionError` (``EX.value-missing``, ``params.element`` =
    element name) when the element has no value yet. The modelling rules
    guarantee the write before the decision, so this means the model bypassed
    validation.
    """

    if discriminator not in instance.data_values:
        raise ExecutionError(
            f"{subject} needs data element '{discriminator}' but it is not set",
            code="EX.value-missing",
            params={"element": _element(schema, discriminator)},
        )
    return instance.data_values[discriminator]


def _loop_of_end(schema: ProcessSchema, end_id: str) -> tuple[str, set[str]]:
    """Find the loop block a LOOP_END closes.

    ``model.loop_block`` pairs from the LOOP_START side, so the LOOP_STARTs are
    tried in schema order until the one whose block ends at ``end_id`` is found
    -- the block is computed only once per candidate.

    Returns ``(start_id, body)``: the id of the paired LOOP_START and the node
    ids strictly between start and end. A LOOP_END without a paired start is
    impossible on a K6-valid schema; it would surface as ``StopIteration``
    exactly as the former inline lookup did.
    """

    for start_id, start in schema.nodes.items():
        if start.type is not NodeType.LOOP_START:
            continue
        paired_end, body = loop_block(schema, start_id)
        if paired_end == end_id:
            return start_id, body
    raise StopIteration(f"no LOOP_START pairs with LOOP_END '{end_id}'")


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
    been left). Data values persist across iterations by design and stay
    readable; but from this repetition on a mandatory write of the body is
    not supplied by last round's value: the record of this round's
    corrections is cleared, and the completion check accepts only values the
    round itself brings (K6c makes the discriminator one of them, so each
    round decides on fresh data).

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
        # Every iteration is a fresh offer with fresh clocks: claim, escalation
        # stages and detail overlay belong to the activation, not the node --
        # and so do the data corrections that supplied its mandatory writes.
        _clear_activation(instance, nid)
        _end_corrections(instance, nid)
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
            f"could not be evaluated: {exc}",
            code="EX.follow-up-condition",
            params={},
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
    id is tracked on the source. Each started follow-up is reported to the
    context's ``on_spawn`` observer (if any), with ``instance`` as its origin.

    A follow-up inherits ``is_test`` from ``instance``: what a throw-away test
    instance starts is a test instance too, in both modes. For ASYNC this flag
    is the only tie to the test instance (there is no back-reference), so it
    must be set at birth -- every rule for test instances (no audit, no
    webhooks, no personal worklists, out of the KPIs) then applies unchanged.
    The target itself is RELEASED, so ``allow_unreleased`` stays off.
    """

    for link in schema.follow_up_links:
        if not _follow_up_fires(link, instance):
            continue
        target = context.resolver(link.target_schema_id, link.target_version)
        if target is None:
            raise ExecutionError(
                f"follow-up target '{link.target_schema_id}' cannot be resolved",
                code="EX.target-missing",
                params={},
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
            is_test=instance.is_test,
        )
        instance.follow_up_instances.append(follow_up.id)
        if context.on_spawn is not None:
            context.on_spawn(follow_up, instance)
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
    in place; otherwise the parent waits for the child's later completion. The
    spawned child is reported to the context's ``on_spawn`` observer (if any)
    before the join, with the parent as its origin.

    The child inherits ``is_test`` from the parent, so a sub-process of a test
    instance is a test instance as well -- and, because every instance passes
    its own flag on, so is any grandchild further down the chain.
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
            f"v{binding.target_version} cannot be resolved",
            code="EX.target-missing",
            params={},
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
        is_test=instance.is_test,
    )
    if context.on_spawn is not None:
        context.on_spawn(child, instance)
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


# --- per-activation bookkeeping ----------------------------------------


def _release_claim(instance: ProcessInstance, node_id: str) -> None:
    """Drop the claim of ``node_id`` together with its claim-bound clocks.

    Mutates ``instance`` in place (callers work on their own copy). Removes
    the owner (``claimed_by``), the claim stamp (``node_claimed_at``) and the
    net-time bookkeeping (``node_paused_seconds``/``node_suspended_at``, E2
    Stufe C) -- all of it belongs to one activation (W4) and must never carry
    over to the next owner or round. Used by completion and return; the
    detail overlay is left alone here, since each caller treats it differently
    (a return ends a pause, a completion only happens without one).
    """

    instance.claimed_by.pop(node_id, None)
    instance.node_claimed_at.pop(node_id, None)
    instance.node_paused_seconds.pop(node_id, None)
    instance.node_suspended_at.pop(node_id, None)


def _clear_activation(instance: ProcessInstance, node_id: str) -> None:
    """Wipe every runtime trace of the current activation of ``node_id``.

    Mutates ``instance`` in place. On top of :func:`_release_claim` it removes
    the fired escalation stages (T3/E9: each activation measures its own
    target time) and the detail overlay with its reason (E2). This is the
    common ground of a recovery reset (V4) and a loop reset (K6): in both
    cases the node becomes a fresh offer with fresh clocks. Markings and child
    instance links are the caller's business.
    """

    _release_claim(instance, node_id)
    instance.escalated_stages.pop(node_id, None)
    instance.node_details.pop(node_id, None)
    instance.node_detail_reason.pop(node_id, None)


def _end_corrections(instance: ProcessInstance, node_id: str) -> None:
    """Drop the data corrections recorded for the activation of ``node_id``.

    Mutates ``instance`` in place. The one place where the record of
    :func:`record_data_correction` ends -- when the activation it belongs to
    ends: the step's completion (the values are used up; its next run, a
    later loop round, must be supplied afresh) and the loop-iteration reset
    (the next round is a new activation).

    Deliberately **not** part of :func:`_clear_activation`: its other caller,
    the recovery reset of a failed step (:func:`reset_activity`), keeps the
    step in the same round. A correction made to repair what made the step
    fail is exactly the value the second attempt is meant to use; dropping
    it there would refuse the repaired step with ``D1.write-missing``.

    :param instance: the case (its own copy -- changed in place).
    :param node_id: the step whose activation ended. A step without a record
        is left as it is.
    """

    instance.data_corrected_in_activation.pop(node_id, None)


# --- guards --------------------------------------------------------------


def _require_running(instance: ProcessInstance) -> None:
    if instance.state is not InstanceState.RUNNING:
        raise ExecutionError(
            f"instance '{instance.id}' is not running (state {instance.state.value})",
            code="EX.not-running",
            params={},
        )


def _require_activity(schema: ProcessSchema, node_id: str) -> Node:
    node = schema.nodes.get(node_id)
    if node is None:
        raise ExecutionError(
            f"node '{node_id}' does not exist",
            code="EX.unknown-step",
            params={},
        )
    if node.type is not NodeType.ACTIVITY:
        raise ExecutionError(
            f"node '{node_id}' is not an ACTIVITY",
            code="EX.not-activity",
            params={"step": _step(schema, node_id)},
        )
    return node
