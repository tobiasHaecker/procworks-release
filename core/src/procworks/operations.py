# SPDX-License-Identifier: BUSL-1.1
"""High-level change operations (Section 7).

These are the *only* way to mutate a schema. Each operation:
  1. checks its preconditions (``requires``),
  2. produces a candidate schema,
  3. validates it (validate-before-commit) and only then returns it.

Because the operations always construct balanced blocks and every result is
validated, an incorrect schema can never be produced or persisted -- this is
Correctness by Construction in practice.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from procworks.model import (
    JOIN_TYPES,
    SPLIT_TYPES,
    AccessMode,
    ActivityTemplate,
    Agent,
    AggregateKind,
    AutomationKind,
    Cardinality,
    ConnectorDescriptor,
    ConnectorKind,
    ControlEdge,
    DataAccess,
    DataElement,
    DataSourceKind,
    DataType,
    EdgeType,
    EscalationPolicy,
    ExecutorKind,
    ExternalBinding,
    FollowUpLink,
    FollowUpMode,
    FollowUpTrigger,
    Form,
    FormField,
    LifecycleState,
    LoopCell,
    LoopDecision,
    MailBinding,
    Node,
    NodeType,
    OrderBy,
    OrgModel,
    OrgUnit,
    ProcessSchema,
    ProcessTemplate,
    QueryFilter,
    Role,
    ServiceBinding,
    SqlSelectBinding,
    SqlWriteBinding,
    StaffRule,
    SubProcessBinding,
    TemplateOrigin,
    TemplateParameter,
    TimeConstraint,
    ValueClass,
    WidgetKind,
    WorkItemPriority,
    XorBranch,
    XorDecision,
    block_join,
    discriminator_kind,
    loop_block,
    xor_condition_text,
)
from procworks.validator import (
    CorrectnessError,
    SchemaResolver,
    ValidationFinding,
    check_executable,
    raise_if_invalid,
)

_counter = itertools.count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{next(_counter)}"


def create_empty_schema(name: str, schema_id: str | None = None) -> ProcessSchema:
    """Create the minimal correct schema: START -> END."""

    start = Node(id="start", type=NodeType.START, label="Start")
    end = Node(id="end", type=NodeType.END, label="Ende")
    schema = ProcessSchema(
        id=schema_id or _new_id("schema"),
        name=name,
        nodes={start.id: start, end.id: end},
        edges=[ControlEdge(source=start.id, target=end.id)],
    )
    return raise_if_invalid(schema)


def _require_editable(schema: ProcessSchema) -> None:
    if schema.lifecycle_state is not LifecycleState.ENTWURF:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="R0",
                    message=(
                        f"schema is {schema.lifecycle_state.value}; only ENTWURF is editable"
                    ),
                )
            ]
        )


def _require_local_org(schema: ProcessSchema) -> None:
    """Reject editing the embedded org model when a shared one is linked.

    A schema that references a shared org model (``org_model_id`` set) must not
    have its (hydrated) org master data edited in place; the shared model is
    the single source of truth and is edited via the org operations / endpoints.
    """

    if schema.org_model_id is not None:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    message=(
                        "schema uses a shared organisation; edit it via the shared "
                        "org model instead"
                    ),
                )
            ]
        )


def _require_node(schema: ProcessSchema, node_id: str) -> Node:
    node = schema.nodes.get(node_id)
    if node is None:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"node '{node_id}' does not exist")]
        )
    return node


def _single_outgoing(schema: ProcessSchema, node_id: str) -> ControlEdge:
    out = schema.outgoing(node_id)
    if len(out) != 1:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message=(
                        f"insertion anchor must have exactly one outgoing edge (has {len(out)})"
                    ),
                )
            ]
        )
    return out[0]


def serial_insert(schema: ProcessSchema, label: str, after_node_id: str) -> ProcessSchema:
    """Insert a single ACTIVITY sequentially after ``after_node_id``.

    requires: schema editable; anchor exists and is not END; anchor has one
              outgoing edge.
    ensures:  new activity spliced between anchor and its successor; K1-K3 hold.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    anchor = _require_node(candidate, after_node_id)
    if anchor.type is NodeType.END:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", node_id=after_node_id, message="cannot insert after END")]
        )
    edge = _single_outgoing(candidate, after_node_id)
    successor_id = edge.target

    new_node = Node(id=_new_id("act"), type=NodeType.ACTIVITY, label=label)
    candidate.nodes[new_node.id] = new_node
    candidate.edges.remove(edge)
    candidate.edges.append(ControlEdge(source=after_node_id, target=new_node.id))
    candidate.edges.append(ControlEdge(source=new_node.id, target=successor_id))
    return raise_if_invalid(candidate)


def parallel_insert(
    schema: ProcessSchema, branch_labels: list[str], after_node_id: str
) -> ProcessSchema:
    """Insert a balanced AND block (one activity per branch) after the anchor.

    requires: schema editable; anchor exists and is not END; >= 2 branches.
    ensures:  AND_SPLIT/AND_JOIN with N parallel activity branches; K1-K3 hold.
    """

    if len(branch_labels) < 2:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message="parallel_insert requires at least 2 branches")]
        )
    return _insert_block(schema, after_node_id, NodeType.AND_SPLIT, branch_labels)


@dataclass(frozen=True)
class BranchSpec:
    """One requested XOR branch: a body label plus its partition cell (K7).

    The cell fields are interpreted per the discriminator's derived kind:
    ``upper`` for THRESHOLD (the last branch leaves it ``None`` for ``+inf``),
    ``bool_value`` for BOOLEAN, and ``values``/``is_else`` for ENUM. Callers only
    describe the partition; :func:`conditional_insert` wires the bodies and emits
    the structured :class:`~procworks.model.XorDecision`.
    """

    label: str
    upper: float | None = None
    bool_value: bool | None = None
    values: tuple[str, ...] = ()
    is_else: bool = False


def conditional_insert(
    schema: ProcessSchema,
    after_node_id: str,
    *,
    discriminator: str,
    branches: list[BranchSpec],
) -> ProcessSchema:
    """Insert a balanced XOR block governed by a structured partition (K7).

    ``discriminator`` is the id of an instance data element whose typed value
    selects the branch; ``branches`` describe a *total, disjoint* partition of
    that element's domain (the kind -- THRESHOLD/BOOLEAN/ENUM -- is derived from
    the element's type). The resulting :class:`XorDecision` makes exactly one
    branch enabled for every value, so the split can neither deadlock nor
    activate several paths -- and the validator (K7) refuses any other shape.

    requires: schema editable; anchor exists and is not END; discriminator is a
              partitionable instance element; >= 2 branches forming a valid
              partition for the derived kind.
    ensures:  XOR_SPLIT/XOR_JOIN with one body per branch, a stored XorDecision,
              and derived edge captions; K1-K3, K7 and the data-flow rules hold.
    """

    if len(branches) < 2:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP", message="conditional_insert requires at least 2 branches"
                )
            ]
        )

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    anchor = _require_node(candidate, after_node_id)
    if anchor.type is NodeType.END:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", node_id=after_node_id, message="cannot insert after END")]
        )
    element = candidate.data_elements.get(discriminator)
    if element is None:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    message=f"unknown discriminator data element '{discriminator}'",
                )
            ]
        )
    kind = discriminator_kind(element.data_type)
    if kind is None:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    message=(
                        f"data type {element.data_type.value} "
                        "cannot be used as an XOR discriminator"
                    ),
                )
            ]
        )

    edge = _single_outgoing(candidate, after_node_id)
    successor_id = edge.target

    split = Node(id=_new_id("split"), type=NodeType.XOR_SPLIT)
    join = Node(id=_new_id("join"), type=NodeType.XOR_JOIN)
    candidate.nodes[split.id] = split
    candidate.nodes[join.id] = join
    candidate.edges.remove(edge)
    candidate.edges.append(ControlEdge(source=after_node_id, target=split.id))
    candidate.edges.append(ControlEdge(source=join.id, target=successor_id))

    xor_branches: list[XorBranch] = []
    body_edges: list[ControlEdge] = []
    for spec in branches:
        body = Node(id=_new_id("act"), type=NodeType.ACTIVITY, label=spec.label)
        candidate.nodes[body.id] = body
        xor_branches.append(
            XorBranch(
                target=body.id,
                upper=spec.upper,
                bool_value=spec.bool_value,
                values=list(spec.values),
                is_else=spec.is_else,
            )
        )
        split_edge = ControlEdge(source=split.id, target=body.id)
        candidate.edges.append(split_edge)
        candidate.edges.append(ControlEdge(source=body.id, target=join.id))
        body_edges.append(split_edge)

    decision = XorDecision(discriminator=discriminator, kind=kind, branches=xor_branches)
    candidate.xor_decisions[split.id] = decision
    for index, split_edge in enumerate(body_edges):
        split_edge.condition = xor_condition_text(element.name, decision, index)

    return raise_if_invalid(candidate)


def _insert_block(
    schema: ProcessSchema,
    after_node_id: str,
    split_type: NodeType,
    branch_labels: list[str],
) -> ProcessSchema:
    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    anchor = _require_node(candidate, after_node_id)
    if anchor.type is NodeType.END:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", node_id=after_node_id, message="cannot insert after END")]
        )
    edge = _single_outgoing(candidate, after_node_id)
    successor_id = edge.target

    join_type = {
        NodeType.AND_SPLIT: NodeType.AND_JOIN,
        NodeType.XOR_SPLIT: NodeType.XOR_JOIN,
    }[split_type]

    split = Node(id=_new_id("split"), type=split_type)
    join = Node(id=_new_id("join"), type=join_type)
    candidate.nodes[split.id] = split
    candidate.nodes[join.id] = join

    candidate.edges.remove(edge)
    candidate.edges.append(ControlEdge(source=after_node_id, target=split.id))
    candidate.edges.append(ControlEdge(source=join.id, target=successor_id))

    for label in branch_labels:
        branch = Node(id=_new_id("act"), type=NodeType.ACTIVITY, label=label)
        candidate.nodes[branch.id] = branch
        candidate.edges.append(ControlEdge(source=split.id, target=branch.id))
        candidate.edges.append(ControlEdge(source=branch.id, target=join.id))

    return raise_if_invalid(candidate)


def _build_loop_decision(
    schema: ProcessSchema,
    discriminator: str,
    repeat_value: bool,
    cells: Sequence[LoopCell] | None,
    max_iterations: int | None,
) -> LoopDecision:
    """Assemble a :class:`LoopDecision` from operation arguments (K6b).

    Shared by :func:`insert_loop` and :func:`set_loop_decision`. Without
    ``cells`` the boolean S1 shorthand is built (the discriminator must be a
    BOOLEAN INSTANCE element); with ``cells`` the S3 repeat/exit partition is
    built and the kind derives from the element's type -- wellformedness of the
    partition itself is the validator's job (K6b, validate-before-commit).
    """

    element = schema.data_elements.get(discriminator)
    if element is None:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    message=f"loop discriminator '{discriminator}' does not exist",
                )
            ]
        )
    if element.source is not DataSourceKind.INSTANCE:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    message=(
                        f"loop discriminator '{element.name}' must be an "
                        "INSTANCE element"
                    ),
                )
            ]
        )
    if not cells:
        if element.data_type is not DataType.BOOLEAN:
            raise CorrectnessError(
                [
                    ValidationFinding(
                        rule="OP",
                        message=(
                            f"loop discriminator '{element.name}' must be BOOLEAN "
                            "unless repeat/exit cells are given"
                        ),
                    )
                ]
            )
        return LoopDecision(
            discriminator=discriminator,
            repeat_value=repeat_value,
            max_iterations=max_iterations,
        )
    kind = discriminator_kind(element.data_type)
    if kind is None:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    message=(
                        f"data type {element.data_type.value} cannot be used "
                        "as a loop discriminator"
                    ),
                )
            ]
        )
    return LoopDecision(
        discriminator=discriminator,
        kind=kind,
        cells=[cell.model_copy(deep=True) for cell in cells],
        max_iterations=max_iterations,
    )


def insert_loop(
    schema: ProcessSchema,
    after_node_id: str,
    body_label: str,
    *,
    discriminator: str,
    repeat_value: bool = True,
    cells: Sequence[LoopCell] | None = None,
    max_iterations: int | None = None,
) -> ProcessSchema:
    """Insert a REPEAT-UNTIL loop block after the anchor (K6).

    ``max_iterations`` (optional) caps the total number of body runs as a
    deterministic hard brake and makes the T2 critical path charge the body
    that many times; at least 2 when set (K6b).

    Builds ``LOOP_START -> body activity -> LOOP_END`` on the anchor's outgoing
    edge and stores the structured :class:`LoopDecision` on the LOOP_END.
    Without ``cells`` the boolean shorthand applies: when the BOOLEAN
    ``discriminator`` equals ``repeat_value`` at the loop end, the body runs
    again, otherwise the flow leaves the block. With ``cells`` (stage S3) the
    discriminator may be any partitionable type (INTEGER/FLOAT -> THRESHOLD,
    BOOLEAN, STRING -> ENUM) and the cells classify its domain into repeat and
    exit -- same wellformedness as a K7 partition, plus at least one cell of
    each class (K6b). The loop-back edge is **not stored** -- it derives from
    the K6 pairing, keeping the graph acyclic.

    Automatic structure completion (the loop brings its decidability along):
    the fresh body activity receives a mandatory WRITE access on the
    discriminator, because K6c demands a write on every body path and an empty
    body write-set would make the operation unusable. The modeller may later
    relocate that write onto another body node; only removing it without a
    replacement is rejected (K6c).

    requires: schema editable (R0); anchor exists, is not END and has exactly
              one outgoing edge; ``discriminator`` is an existing INSTANCE
              data element (BOOLEAN without ``cells``, any partitionable type
              with ``cells``).
    ensures:  a properly paired, decidable, non-empty loop block sits on the
              anchor's outgoing edge; the full rule catalogue holds
              (validate-before-commit).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    anchor = _require_node(candidate, after_node_id)
    if anchor.type is NodeType.END:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP", node_id=after_node_id, message="cannot insert after END"
                )
            ]
        )
    decision = _build_loop_decision(
        candidate, discriminator, repeat_value, cells, max_iterations
    )
    edge = _single_outgoing(candidate, after_node_id)
    successor_id = edge.target

    loop_start = Node(id=_new_id("loop"), type=NodeType.LOOP_START, label="Wiederholen")
    body = Node(id=_new_id("act"), type=NodeType.ACTIVITY, label=body_label)
    loop_end = Node(id=_new_id("loopend"), type=NodeType.LOOP_END, label="Bis erfüllt")
    candidate.nodes[loop_start.id] = loop_start
    candidate.nodes[body.id] = body
    candidate.nodes[loop_end.id] = loop_end
    candidate.edges.remove(edge)
    candidate.edges.append(ControlEdge(source=after_node_id, target=loop_start.id))
    candidate.edges.append(ControlEdge(source=loop_start.id, target=body.id))
    candidate.edges.append(ControlEdge(source=body.id, target=loop_end.id))
    candidate.edges.append(ControlEdge(source=loop_end.id, target=successor_id))
    candidate.loop_decisions[loop_end.id] = decision
    candidate.data_accesses.append(
        DataAccess(
            node_id=body.id,
            element_id=discriminator,
            mode=AccessMode.WRITE,
            mandatory=True,
        )
    )
    return raise_if_invalid(candidate)


def set_loop_decision(
    schema: ProcessSchema,
    loop_end_id: str,
    *,
    discriminator: str,
    repeat_value: bool = True,
    cells: Sequence[LoopCell] | None = None,
    max_iterations: int | None = None,
) -> ProcessSchema:
    """Replace the exit condition of an existing loop (K6b).

    Lets the modeller retarget the decision to another discriminator, flip the
    boolean shorthand, switch between the shorthand and an S3 repeat/exit
    partition, or set/clear the ``max_iterations`` hard brake -- without
    rebuilding the loop. The body must (still) write the new discriminator on
    every path; otherwise K6c rejects the change and the schema stays
    untouched (validate-before-commit).

    requires: schema editable (R0); ``loop_end_id`` is a LOOP_END node;
              ``discriminator`` as in :func:`insert_loop`.
    ensures:  the LOOP_END carries the new decision and the full rule
              catalogue holds.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, loop_end_id)
    if node.type is not NodeType.LOOP_END:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=loop_end_id,
                    message="a loop decision can only be set on a LOOP_END node",
                )
            ]
        )
    candidate.loop_decisions[loop_end_id] = _build_loop_decision(
        candidate, discriminator, repeat_value, cells, max_iterations
    )
    return raise_if_invalid(candidate)


def add_sync_edge(
    schema: ProcessSchema, source_id: str, target_id: str
) -> ProcessSchema:
    """Add a K4 synchronisation edge between two parallel activities.

    The target then additionally waits until the source is completed or
    deselected -- an ordering-only constraint that never routes tokens.
    Whether the pair really lies in different branches of one AND block and
    whether the combined graph stays acyclic is the validator's job (K4,
    validate-before-commit): an ill-placed sync edge can never be stored.

    requires: schema editable (R0); both nodes exist; no duplicate edge.
    ensures:  the SYNC edge is stored and the full rule catalogue holds.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    _require_node(candidate, source_id)
    _require_node(candidate, target_id)
    if source_id == target_id:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=source_id,
                    message="a sync edge cannot connect a node with itself",
                )
            ]
        )
    if any(
        e.type is EdgeType.SYNC and e.source == source_id and e.target == target_id
        for e in candidate.edges
    ):
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=source_id,
                    message="this sync edge already exists",
                )
            ]
        )
    candidate.edges.append(
        ControlEdge(source=source_id, target=target_id, type=EdgeType.SYNC)
    )
    return raise_if_invalid(candidate)


def remove_sync_edge(
    schema: ProcessSchema, source_id: str, target_id: str
) -> ProcessSchema:
    """Remove a K4 synchronisation edge (inverse of :func:`add_sync_edge`)."""

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    before = len(candidate.edges)
    candidate.edges = [
        e
        for e in candidate.edges
        if not (
            e.type is EdgeType.SYNC
            and e.source == source_id
            and e.target == target_id
        )
    ]
    if len(candidate.edges) == before:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=source_id,
                    message="no such sync edge",
                )
            ]
        )
    return raise_if_invalid(candidate)


def _and_block_of(
    schema: ProcessSchema, member_ids: set[str]
) -> tuple[str, str] | None:
    """Innermost AND block (split, join) whose branches cover ``member_ids``.

    Walks every AND split's branches (depth-counted, like the K4 checker) and
    picks the smallest block that contains all members -- the innermost one.
    ``None`` when no single AND block covers the set.
    """

    best: tuple[int, str, str] | None = None
    for node in schema.nodes.values():
        if node.type is not NodeType.AND_SPLIT:
            continue
        covered: set[str] = set()
        join_id: str | None = None
        stack = [(e.target, 0) for e in schema.outgoing(node.id)]
        while stack:
            current, depth = stack.pop()
            member = schema.nodes.get(current)
            if member is None or current in covered:
                continue
            if member.type in JOIN_TYPES and depth == 0:
                join_id = current
                continue
            covered.add(current)
            next_depth = depth
            if member.type in SPLIT_TYPES:
                next_depth += 1
            elif member.type in JOIN_TYPES:
                next_depth -= 1
            for out in schema.outgoing(current):
                stack.append((out.target, next_depth))
        if join_id is not None and member_ids <= covered:
            size = len(covered)
            if best is None or size < best[0]:
                best = (size, node.id, join_id)
    if best is None:
        return None
    return best[1], best[2]


def insert_between_node_sets(
    schema: ProcessSchema,
    label: str,
    source_ids: list[str],
    target_ids: list[str],
) -> ProcessSchema:
    """Insert an activity between two node sets (ADEPT ``insertBetweenNodeSets``).

    The new activity becomes a **fresh branch** of the innermost AND block
    that contains all sources and targets, and is wired with K4 sync edges:
    it starts only after every source is resolved and every target
    additionally waits for it. Because the new branch differs from every
    existing one, each sync edge crosses branches by construction (K4); the
    acyclicity of the requested ordering is validated before commit.

    requires: schema editable (R0); both sets non-empty; all set members are
              existing ACTIVITY nodes inside one common AND block.
    ensures:  new branch ``split -> activity -> join`` plus the sync wiring;
              the full rule catalogue holds.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    if not label.strip():
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message="the new activity needs a label")]
        )
    if not source_ids or not target_ids:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    message="insert_between_node_sets needs a non-empty source "
                    "and target set",
                )
            ]
        )
    members = set(source_ids) | set(target_ids)
    for node_id in sorted(members):
        node = _require_node(candidate, node_id)
        if node.type is not NodeType.ACTIVITY:
            raise CorrectnessError(
                [
                    ValidationFinding(
                        rule="OP",
                        node_id=node_id,
                        message="source/target sets may only contain ACTIVITY nodes",
                    )
                ]
            )
    block = _and_block_of(candidate, members)
    if block is None:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    message=(
                        "all sources and targets must lie inside one common "
                        "AND block (K4)"
                    ),
                )
            ]
        )
    split_id, join_id = block
    activity = Node(id=_new_id("act"), type=NodeType.ACTIVITY, label=label)
    candidate.nodes[activity.id] = activity
    candidate.edges.append(ControlEdge(source=split_id, target=activity.id))
    candidate.edges.append(ControlEdge(source=activity.id, target=join_id))
    for source_id in source_ids:
        candidate.edges.append(
            ControlEdge(source=source_id, target=activity.id, type=EdgeType.SYNC)
        )
    for target_id in target_ids:
        candidate.edges.append(
            ControlEdge(source=activity.id, target=target_id, type=EdgeType.SYNC)
        )
    return raise_if_invalid(candidate)


def rename_node(schema: ProcessSchema, node_id: str, label: str) -> ProcessSchema:
    """Change the label of an ACTIVITY or SUBPROCESS node.

    requires: schema editable (R0); node exists and is an ACTIVITY or
              SUBPROCESS (START/END and gateways carry generated captions, so
              they are not renamable).
    ensures:  the node's label is updated; the schema stays correct (K/D/Z
              unaffected -- relabelling never changes structure).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type not in (NodeType.ACTIVITY, NodeType.SUBPROCESS):
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="only ACTIVITY or SUBPROCESS nodes can be renamed",
                )
            ]
        )
    node.label = label
    return raise_if_invalid(candidate)


def move_node(schema: ProcessSchema, node_id: str, after_node_id: str) -> ProcessSchema:
    """Move an existing ACTIVITY/SUBPROCESS to a new serial position.

    Unlike delete + re-insert, the node travels with **all** its bindings
    (data accesses, staff rule, service binding, form, mail binding, time
    constraint, priority, value class) -- they hang off the node id, which is
    preserved. Structurally the node is spliced out of its current position
    (predecessor reconnected to successor) and spliced back in between
    ``after_node_id`` and that anchor's successor: the serial mechanics of
    :func:`delete_node` and :func:`serial_insert` composed into one atomic,
    validated operation.

    requires: schema editable (R0); node exists and is an ACTIVITY or
              SUBPROCESS on a serial stretch (one in/one out); the anchor
              exists, is not END, is not the node itself and -- after the
              splice-out -- has exactly one outgoing edge (so a split can never
              be an anchor). Moving the sole node of an **AND** branch is
              rejected (an empty parallel branch is meaningless; delete it or
              grow the branch first).
    ensures:  the node sits between the anchor and the anchor's former
              successor. Moving the sole node of an **XOR** branch leaves the
              branch standing **empty** (direct ``split -> join`` edge keeping
              its K7 cell) -- exactly the :func:`delete_node` semantics -- and
              is subject to the same one-empty-branch cap. If the node opened a
              (longer) XOR branch, the branch's partition cell is retargeted to
              the node's former successor and the derived edge captions are
              refreshed. The full rule catalogue re-validates before commit:
              a move that would let a reader run before its writer (D1), break
              a staff-rule back-reference (Z3), reorder a time-critical path
              (T2) or orphan a mail placeholder (N4) is rejected with localised
              findings and the schema stays unchanged. Moving a node directly
              behind its current predecessor is an allowed no-op.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type not in (NodeType.ACTIVITY, NodeType.SUBPROCESS):
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="only ACTIVITY or SUBPROCESS nodes can be moved",
                )
            ]
        )
    if node_id == after_node_id:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="cannot move a node after itself",
                )
            ]
        )
    anchor = _require_node(candidate, after_node_id)
    if anchor.type is NodeType.END:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP", node_id=after_node_id, message="cannot insert after END"
                )
            ]
        )

    incoming = candidate.incoming(node_id)
    outgoing = candidate.outgoing(node_id)
    if len(incoming) != 1 or len(outgoing) != 1:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message=(
                        f"node '{node_id}' is not on a serial stretch (one in/one out)"
                    ),
                )
            ]
        )
    predecessor_id = incoming[0].source
    successor_id = outgoing[0].target
    pred_type = candidate.nodes[predecessor_id].type
    if (
        pred_type is NodeType.LOOP_START
        and candidate.nodes[successor_id].type is NodeType.LOOP_END
    ):
        # Sole content of a loop body: moving it out would leave an empty loop
        # (meaningless, K6d). Mirrors the AND-branch rule.
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message=(
                        "cannot move the sole node of a loop body; "
                        "delete the loop or add another node first"
                    ),
                )
            ]
        )

    # Splice out. Three cases for the gap left behind, mirroring delete_node:
    # sole content of an AND branch (rejected), sole content of an XOR branch
    # (branch stays standing but empty, keeping its K7 cell), or a plain serial
    # stretch (predecessor reconnected to successor).
    sole_branch_content = False
    if pred_type in SPLIT_TYPES and candidate.nodes[successor_id].type in JOIN_TYPES:
        join_id, _ = _matching_block(candidate, predecessor_id)
        sole_branch_content = join_id == successor_id
    decision = candidate.xor_decisions.get(predecessor_id)
    if sole_branch_content:
        if pred_type is not NodeType.XOR_SPLIT:
            raise CorrectnessError(
                [
                    ValidationFinding(
                        rule="OP",
                        node_id=node_id,
                        message=(
                            "cannot move the sole node of a parallel branch; "
                            "delete the branch or add another node first"
                        ),
                    )
                ]
            )
        if decision is not None and any(
            b.target == successor_id for b in decision.branches
        ):
            raise CorrectnessError(
                [
                    ValidationFinding(
                        rule="OP",
                        node_id=predecessor_id,
                        message=(
                            "an XOR split must keep at least one non-empty branch; "
                            "remove the whole branch block instead"
                        ),
                    )
                ]
            )
    candidate.edges = [
        e for e in candidate.edges if e.source != node_id and e.target != node_id
    ]
    candidate.edges.append(ControlEdge(source=predecessor_id, target=successor_id))
    if decision is not None:
        # The node opened an XOR branch: its partition cell now starts at the
        # former successor (the matching join itself if the branch ran empty).
        for branch in decision.branches:
            if branch.target == node_id:
                branch.target = successor_id
                break
        _refresh_xor_captions(candidate, predecessor_id)

    # Splice in after the anchor. If the anchor was the node's predecessor the
    # single outgoing edge is the gap-closing edge just added -- the move
    # degenerates to a no-op, which is fine.
    edge = _single_outgoing(candidate, after_node_id)
    candidate.edges.remove(edge)
    candidate.edges.append(
        ControlEdge(source=after_node_id, target=node_id, condition=edge.condition)
    )
    candidate.edges.append(ControlEdge(source=node_id, target=edge.target))
    return raise_if_invalid(candidate)


def _matching_block(schema: ProcessSchema, split_id: str) -> tuple[str, set[str]]:
    """Return ``(matching_join_id, inner_node_ids)`` for a split gateway.

    Thin wrapper around :func:`procworks.model.block_join` -- the single source
    of truth for "which join closes this block", shared with the validator's K1
    nesting check so operations and validation can never disagree. The inner ids
    are the branch bodies strictly between split and join (both gateways
    excluded).

    On a K1-valid schema the block is unambiguous, so this never raises in
    normal operation; the guard remains because a caller that reached here with
    a crossed block must fail loudly rather than silently delete an arbitrary
    region.
    """

    try:
        matching_join, branches = block_join(schema, split_id)
    except ValueError as exc:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", node_id=split_id, message=str(exc))]
        ) from exc
    inner: set[str] = set().union(*branches) if branches else set()
    return matching_join, inner


def _drop_nodes(candidate: ProcessSchema, to_remove: set[str]) -> None:
    """Remove ``to_remove`` together with their dependent bindings/accesses."""

    for removed in to_remove:
        del candidate.nodes[removed]
        candidate.staff_rules.pop(removed, None)
        candidate.service_bindings.pop(removed, None)
        candidate.sub_process_bindings.pop(removed, None)
        candidate.xor_decisions.pop(removed, None)
        candidate.loop_decisions.pop(removed, None)
        candidate.forms.pop(removed, None)
        # Time/mail/priority annotations would otherwise survive as stale keys
        # -- and T1/N2 flag unknown nodes, so a node carrying one could never
        # be deleted at all (validate-before-commit rejected the deletion).
        candidate.time_constraints.pop(removed, None)
        candidate.mail_bindings.pop(removed, None)
        candidate.node_priorities.pop(removed, None)
        candidate.escalation_policies.pop(removed, None)  # T3: same lesson
    candidate.data_accesses = [
        a for a in candidate.data_accesses if a.node_id not in to_remove
    ]


def _refresh_xor_captions(candidate: ProcessSchema, split_id: str) -> None:
    """Regenerate the display captions on all edges leaving an XOR split.

    The structured :class:`XorDecision` is the source of truth; each branch's
    outgoing edge (matched by target -- unique, because a split carries at most
    one empty branch) gets its derived predicate text refreshed. Called after a
    branch is emptied or removed so the captions stay in sync with the (possibly
    reordered) partition. Silently returns if the split has no decision or its
    discriminator element is gone (both are separately flagged by K7).
    """

    decision = candidate.xor_decisions.get(split_id)
    if decision is None:
        return
    element = candidate.data_elements.get(decision.discriminator)
    if element is None:
        return
    edge_by_target = {e.target: e for e in candidate.outgoing(split_id)}
    for index, branch in enumerate(decision.branches):
        edge = edge_by_target.get(branch.target)
        if edge is not None:
            edge.condition = xor_condition_text(element.name, decision, index)


def _empty_out_xor_branch(
    candidate: ProcessSchema, node_id: str, split_id: str, join_id: str
) -> ProcessSchema:
    """Empty an XOR branch instead of removing it: keep it as ``split -> join``.

    Removing the sole activity of an XOR branch leaves the branch **standing but
    empty** -- a direct ``split -> join`` edge whose :class:`XorBranch` keeps its
    partition cell. Because the cell is retained, the XOR partition (K7) stays
    total and disjoint automatically, and the modeller can express "only in one
    branch does work occur" (the other branch simply skips to the join).

    An XOR split may carry **at most one** empty branch (it must keep at least
    one non-empty branch): emptying a branch when another is already empty is
    rejected -- remove the whole branch block instead. This one-empty cap keeps
    the ``split -> join`` edge unique (the runtime keys edges by source+target),
    so an empty branch can never collide with a second one.
    """

    decision = candidate.xor_decisions.get(split_id)
    if decision is not None and any(b.target == join_id for b in decision.branches):
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=split_id,
                    message=(
                        "an XOR split must keep at least one non-empty branch; "
                        "remove the whole branch block instead"
                    ),
                )
            ]
        )

    # Drop the node's own edges (split -> node, node -> join) ...
    candidate.edges = [
        e for e in candidate.edges if e.source != node_id and e.target != node_id
    ]
    # ... retarget the branch onto the join (keeping its K7 cell) and wire the
    # empty branch as a direct split -> join edge.
    if decision is not None:
        for branch in decision.branches:
            if branch.target == node_id:
                branch.target = join_id
                break
    candidate.edges.append(ControlEdge(source=split_id, target=join_id))
    _drop_nodes(candidate, {node_id})
    _refresh_xor_captions(candidate, split_id)
    return raise_if_invalid(candidate)


def _remove_and_branch(
    candidate: ProcessSchema, node_id: str, split_id: str, join_id: str
) -> ProcessSchema:
    """Remove the AND branch ``split -> node -> join`` whose sole content is ``node``.

    For a parallel (AND) split an empty branch carries no meaning (every branch
    runs anyway), so removing the last node removes the branch itself. If more
    than one branch survives, the gateway is kept with that branch gone; if
    exactly one branch remains, the whole gateway is dissolved and the surviving
    branch is spliced inline between the split's predecessor and the join's
    successor.
    """

    candidate.edges = [
        e for e in candidate.edges if e.source != node_id and e.target != node_id
    ]
    remaining = candidate.outgoing(split_id)
    if len(remaining) >= 2:
        # The gateway still has at least two branches -> just drop this one.
        _drop_nodes(candidate, {node_id})
        return raise_if_invalid(candidate)

    # Only a single branch survives -> dissolve the gateway and keep that branch.
    predecessor_id = candidate.incoming(split_id)[0].source
    successor_id = candidate.outgoing(join_id)[0].target
    head_id = remaining[0].target
    tail_id = candidate.incoming(join_id)[0].source
    to_remove = {node_id, split_id, join_id}
    candidate.edges = [
        e
        for e in candidate.edges
        if e.source not in {split_id, join_id} and e.target not in {split_id, join_id}
    ]
    candidate.edges.append(ControlEdge(source=predecessor_id, target=head_id))
    candidate.edges.append(ControlEdge(source=tail_id, target=successor_id))
    _drop_nodes(candidate, to_remove)
    return raise_if_invalid(candidate)


def _delete_single_node_branch(
    candidate: ProcessSchema, node_id: str, split_id: str, join_id: str
) -> ProcessSchema:
    """Delete the sole content ``node`` of a gateway branch ``split -> node -> join``.

    Dispatches on the gateway kind: for an **XOR** split the branch is kept as an
    empty ``split -> join`` branch (see :func:`_empty_out_xor_branch`), so the
    modeller can model "work only in one branch"; for an **AND** split the branch
    is removed outright (see :func:`_remove_and_branch`), because an empty
    parallel branch is meaningless.
    """

    if candidate.nodes[split_id].type is NodeType.XOR_SPLIT:
        return _empty_out_xor_branch(candidate, node_id, split_id, join_id)
    return _remove_and_branch(candidate, node_id, split_id, join_id)


def delete_node(schema: ProcessSchema, node_id: str) -> ProcessSchema:
    """Delete a node from a draft, closing the resulting gap.

    requires: schema editable (R0); node exists and is not START/END; a JOIN
              must be removed via its opening SPLIT, not directly; an
              ACTIVITY/SUBPROCESS must sit on a serial stretch (one in, one
              out).
    ensures:  for an ACTIVITY/SUBPROCESS the predecessor is reconnected to the
              successor; for a SPLIT the whole balanced block (split, branch
              bodies and matching join) is removed as a unit and the gap is
              closed. Deleting the sole node of a gateway branch removes that
              branch; if only a single branch then remains, the entire gateway
              (split and matching join) is dissolved and the surviving branch
              is kept inline. Deleting the sole activity of an **XOR** branch is
              different: the branch is kept as an **empty** ``split -> join``
              branch (retaining its K7 cell), so the model can express "work only
              in one branch"; the empty branch is removed on demand via
              :func:`remove_empty_branch`. Dependent data accesses, staff rules
              and service/sub-process bindings of every removed node are dropped.
              The
              result is validated (validate-before-commit): a deletion that
              would orphan a still-needed data write is rejected (D1) -- this
              keeps Correctness by Construction across deletions.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type in (NodeType.START, NodeType.END):
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP", node_id=node_id, message="cannot delete START or END"
                )
            ]
        )
    if node.type in JOIN_TYPES:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="delete the opening split to remove the whole branch block",
                )
            ]
        )
    if node.type is NodeType.LOOP_END:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="delete the LOOP_START to remove the whole loop block",
                )
            ]
        )

    if node.type is NodeType.LOOP_START:
        # Deleting the loop start removes the whole loop block (start, body,
        # end, decision) -- the loop counterpart of deleting a split.
        end_id, body = loop_block(candidate, node_id)
        to_remove = {node_id, end_id} | body
        predecessor_id = candidate.incoming(node_id)[0].source
        successor_id = candidate.outgoing(end_id)[0].target
    elif node.type in SPLIT_TYPES:
        join_id, inner = _matching_block(candidate, node_id)
        to_remove = {node_id, join_id} | inner
        predecessor_id = candidate.incoming(node_id)[0].source
        successor_id = candidate.outgoing(join_id)[0].target
    else:
        incoming = candidate.incoming(node_id)
        outgoing = candidate.outgoing(node_id)
        if len(incoming) != 1 or len(outgoing) != 1:
            raise CorrectnessError(
                [
                    ValidationFinding(
                        rule="OP",
                        node_id=node_id,
                        message=(
                            f"node '{node_id}' is not on a serial stretch "
                            "(one in/one out)"
                        ),
                    )
                ]
            )
        predecessor_id = incoming[0].source
        successor_id = outgoing[0].target
        pred_type = candidate.nodes[predecessor_id].type
        succ_type = candidate.nodes[successor_id].type
        if pred_type in SPLIT_TYPES and succ_type in JOIN_TYPES:
            join_id, _ = _matching_block(candidate, predecessor_id)
            if join_id == successor_id:
                # The node is the sole content of one gateway branch; removing
                # it removes the branch (and dissolves the gateway if only one
                # branch would remain).
                return _delete_single_node_branch(
                    candidate, node_id, predecessor_id, successor_id
                )
        if (
            pred_type is NodeType.LOOP_START
            and succ_type is NodeType.LOOP_END
        ):
            # Sole content of a loop body: an empty loop is meaningless (K6d),
            # so deleting the last body node dissolves the whole loop -- the
            # counterpart of the single-branch gateway dissolution.
            to_remove = {predecessor_id, node_id, successor_id}
            outer_pred = candidate.incoming(predecessor_id)[0].source
            outer_succ = candidate.outgoing(successor_id)[0].target
            _drop_nodes(candidate, to_remove)
            candidate.edges = [
                e
                for e in candidate.edges
                if e.source not in to_remove and e.target not in to_remove
            ]
            candidate.edges.append(
                ControlEdge(source=outer_pred, target=outer_succ)
            )
            return raise_if_invalid(candidate)
        to_remove = {node_id}

    _drop_nodes(candidate, to_remove)
    candidate.edges = [
        e
        for e in candidate.edges
        if e.source not in to_remove and e.target not in to_remove
    ]
    candidate.edges.append(ControlEdge(source=predecessor_id, target=successor_id))
    return raise_if_invalid(candidate)


def remove_empty_branch(schema: ProcessSchema, split_id: str) -> ProcessSchema:
    """Manually remove the empty branch of an XOR split.

    An empty branch (a direct ``split -> join`` edge, left behind when the sole
    activity of a branch was deleted) can be removed on demand. Two outcomes,
    both validate-before-commit:

    - If exactly one non-empty branch would remain, the whole gateway is
      dissolved and that branch is spliced inline between the split's
      predecessor and the join's successor -- the XOR disappears and the model
      is a plain sequence again.
    - If two or more branches remain, only the empty branch's cell is dropped and
      the gateway is kept. The result is fully re-validated: for THRESHOLD the
      freed range merges seamlessly into the neighbouring branch and for an ENUM
      values-branch the values fall through to the catch-all, so K7 stays total;
      dropping a required catch-all is rejected (K7) and the schema is unchanged.

    requires: schema editable (R0); ``split_id`` is an XOR_SPLIT that currently
              carries exactly one empty branch.
    ensures:  the empty branch is gone; K1-K3, K7 and the data-flow rules hold.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, split_id)
    if node.type is not NodeType.XOR_SPLIT:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=split_id,
                    message="only an XOR split can carry an empty branch",
                )
            ]
        )
    join_id, _ = _matching_block(candidate, split_id)
    decision = candidate.xor_decisions.get(split_id)
    empty_branches = (
        [b for b in decision.branches if b.target == join_id] if decision else []
    )
    if not empty_branches:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=split_id,
                    message="this XOR split has no empty branch to remove",
                )
            ]
        )

    # Drop the empty branch: its direct split -> join edge and its partition cell.
    candidate.edges = [
        e
        for e in candidate.edges
        if not (e.source == split_id and e.target == join_id)
    ]
    if decision is not None:
        decision.branches = [b for b in decision.branches if b.target != join_id]

    remaining = candidate.outgoing(split_id)
    if len(remaining) >= 2:
        # The gateway keeps two or more branches -> K7 re-validates the
        # remaining partition (THRESHOLD/ENUM stay total; a lost catch-all is
        # rejected). Captions may shift because a cell was dropped.
        _refresh_xor_captions(candidate, split_id)
        return raise_if_invalid(candidate)

    # Only a single branch remains -> dissolve the gateway and keep it inline.
    predecessor_id = candidate.incoming(split_id)[0].source
    successor_id = candidate.outgoing(join_id)[0].target
    head_id = remaining[0].target
    tail_id = candidate.incoming(join_id)[0].source
    to_remove = {split_id, join_id}
    candidate.edges = [
        e
        for e in candidate.edges
        if e.source not in to_remove and e.target not in to_remove
    ]
    candidate.edges.append(ControlEdge(source=predecessor_id, target=head_id))
    candidate.edges.append(ControlEdge(source=tail_id, target=successor_id))
    _drop_nodes(candidate, to_remove)
    return raise_if_invalid(candidate)


def add_data_element(
    schema: ProcessSchema,
    name: str,
    data_type: DataType,
    element_id: str | None = None,
) -> ProcessSchema:
    """Add a process data element (instance variable).

    requires: schema editable; element id is unique.
    ensures:  a new data element exists; D1-D4 still hold.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    eid = element_id or _new_id("data")
    if eid in candidate.data_elements:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"data element '{eid}' already exists")]
        )
    candidate.data_elements[eid] = DataElement(id=eid, name=name, data_type=data_type)
    return raise_if_invalid(candidate)


def update_data_element(
    schema: ProcessSchema,
    element_id: str,
    *,
    name: str | None = None,
    data_type: DataType | None = None,
) -> ProcessSchema:
    """Rename a data element and/or change its type.

    Only the provided fields change (``None`` keeps the current value). Editing
    the data catalogue is a structural change, so the schema must be in ENTWURF
    (R0). The result is fully validated: changing the type is rejected if it
    would break the type conformance of an existing access (D3), an input-mask
    widget (U2), an XOR partition (K7) or a scalar SQL binding (C4/C7); renaming
    is always structure-neutral.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    element = candidate.data_elements.get(element_id)
    if element is None:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"data element '{element_id}' does not exist")]
        )
    if name is not None:
        element.name = name
    if data_type is not None:
        element.data_type = data_type
    return raise_if_invalid(candidate)


def reset_data_element_source(schema: ProcessSchema, element_id: str) -> ProcessSchema:
    """Turn an EXTERNAL data element back into a plain INSTANCE variable.

    Clears any connector binding (record / scalar select / scalar write) and
    sets the source to INSTANCE. Only ENTWURF (R0); the result is validated so
    the change stays Correct by Construction.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    element = candidate.data_elements.get(element_id)
    if element is None:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"data element '{element_id}' does not exist")]
        )
    element.source = DataSourceKind.INSTANCE
    element.external = None
    element.select = None
    element.write = None
    return raise_if_invalid(candidate)


def delete_data_element(schema: ProcessSchema, element_id: str) -> ProcessSchema:
    """Remove a data element together with its accesses and input-mask fields.

    Only ENTWURF (R0). All data accesses to the element and all input-mask
    fields bound to it are removed (a mask left without any field is removed
    too). The result is validated: if the element is still referenced elsewhere
    -- as an XOR discriminator (K7), as the key of another external binding, in
    a service parameter mapping (I3) or a sub-process mapping (H) -- the
    deletion is rejected and the schema stays unchanged, so those references
    must be cleared first.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    if element_id not in candidate.data_elements:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"data element '{element_id}' does not exist")]
        )
    del candidate.data_elements[element_id]
    candidate.data_accesses = [
        a for a in candidate.data_accesses if a.element_id != element_id
    ]
    for node_id in list(candidate.forms):
        form = candidate.forms[node_id]
        form.fields = [f for f in form.fields if f.element_id != element_id]
        if not form.fields:
            del candidate.forms[node_id]
    return raise_if_invalid(candidate)


def connect_data(
    schema: ProcessSchema,
    node_id: str,
    element_id: str,
    mode: AccessMode,
    *,
    mandatory: bool = True,
    param_type: DataType | None = None,
) -> ProcessSchema:
    """Connect an ACTIVITY to a data element via a read/write access.

    requires: schema editable; node exists and is an ACTIVITY; element exists.
    ensures:  the access is added and the data-flow rules D1-D4 still hold
              (e.g. a mandatory read whose source is not written on all paths
              is rejected with a D1 finding).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type is not NodeType.ACTIVITY:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="data access is only allowed on ACTIVITY nodes",
                )
            ]
        )
    if element_id not in candidate.data_elements:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"data element '{element_id}' does not exist")]
        )
    candidate.data_accesses.append(
        DataAccess(
            node_id=node_id,
            element_id=element_id,
            mode=mode,
            mandatory=mandatory,
            param_type=param_type,
        )
    )
    return raise_if_invalid(candidate)


def disconnect_data(
    schema: ProcessSchema,
    node_id: str,
    element_id: str,
    mode: AccessMode | None = None,
) -> ProcessSchema:
    """Remove a read/write binding (data access) of an element from a node.

    Inverse of :func:`connect_data`. ``mode`` (optional) narrows the removal to
    accesses of exactly that direction; without it every access of the element
    on this node is removed. An input-mask field that backed a removed access is
    dropped too so mask and data flow stay consistent (U3); a mask left without
    any field is removed.

    requires: schema editable; at least one matching access exists.
    ensures:  the matching access(es) are gone and the data-flow rules D1-D4
              still hold. Removing the sole writer of a mandatory read elsewhere
              is rejected with a D1 finding and leaves the schema unchanged.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)

    def matches(a: DataAccess) -> bool:
        return (
            a.node_id == node_id
            and a.element_id == element_id
            and (mode is None or a.mode == mode)
        )

    if not any(matches(a) for a in candidate.data_accesses):
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message=(
                        f"no data access for element '{element_id}' on node '{node_id}'"
                    ),
                )
            ]
        )
    candidate.data_accesses = [a for a in candidate.data_accesses if not matches(a)]

    # Keep any input mask consistent: drop the fields that mapped to a removed
    # access (same node + element, and matching direction when a mode was given).
    form = candidate.forms.get(node_id)
    if form is not None:
        form.fields = [
            f
            for f in form.fields
            if not (f.element_id == element_id and (mode is None or f.mode == mode))
        ]
        if not form.fields:
            del candidate.forms[node_id]
    return raise_if_invalid(candidate)


@dataclass
class FormFieldSpec:
    """Input intent for one field of an input mask (form designer).

    ``label`` defaults to the element's name; ``options`` are the choices of a
    dropdown. ``mode`` decides the data-flow direction: a WRITE field is an
    input that sets the element, a READ field displays a previously written
    value (governed by D1).
    """

    element_id: str
    widget: WidgetKind
    label: str | None = None
    mode: AccessMode = AccessMode.WRITE
    required: bool = True
    options: tuple[str, ...] = ()
    help_text: str | None = None


def set_form(
    schema: ProcessSchema,
    node_id: str,
    *,
    title: str = "",
    fields: list[FormFieldSpec],
) -> ProcessSchema:
    """Design (or replace) the input mask of an ACTIVITY (form designer).

    Every field is a presentation layer over a data access, so this operation
    also (re-)synchronises the node's data accesses for the elements the mask
    manages: a WRITE field yields a write access, a READ field a read access.
    The mask is laid out automatically -- ``fields`` is just an ordered list.

    requires: schema editable; node exists and is an ACTIVITY; at least one
              field; every field's element exists; no element bound twice.
    ensures:  the mask exists, its data accesses are in place and all rules --
              in particular D1 (kein Read ohne vorheriges Set) and U1-U3 --
              still hold.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type is not NodeType.ACTIVITY:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="input masks are only allowed on ACTIVITY nodes",
                )
            ]
        )
    if not fields:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="an input mask needs at least one field",
                )
            ]
        )

    managed: set[str] = set()
    form_fields: list[FormField] = []
    for spec in fields:
        if spec.element_id not in candidate.data_elements:
            raise CorrectnessError(
                [
                    ValidationFinding(
                        rule="OP",
                        node_id=node_id,
                        message=f"data element '{spec.element_id}' does not exist",
                    )
                ]
            )
        if spec.element_id in managed:
            raise CorrectnessError(
                [
                    ValidationFinding(
                        rule="OP",
                        node_id=node_id,
                        message=(
                            f"data element '{spec.element_id}' is bound by more than "
                            "one field"
                        ),
                    )
                ]
            )
        managed.add(spec.element_id)
        label = spec.label if spec.label else candidate.data_elements[spec.element_id].name
        form_fields.append(
            FormField(
                id=_new_id("field"),
                element_id=spec.element_id,
                widget=spec.widget,
                label=label,
                mode=spec.mode,
                required=spec.required,
                options=list(spec.options),
                help_text=spec.help_text,
            )
        )

    # Re-synchronise this node's data accesses for the managed elements so the
    # mask and the data flow stay consistent (rule U3), then let D1-D4 judge.
    candidate.data_accesses = [
        a
        for a in candidate.data_accesses
        if not (a.node_id == node_id and a.element_id in managed)
    ]
    for spec in fields:
        candidate.data_accesses.append(
            DataAccess(
                node_id=node_id,
                element_id=spec.element_id,
                mode=spec.mode,
                mandatory=spec.required,
            )
        )
    candidate.forms[node_id] = Form(node_id=node_id, title=title, fields=form_fields)
    return raise_if_invalid(candidate)


def delete_form(schema: ProcessSchema, node_id: str) -> ProcessSchema:
    """Remove the input mask of a node and the accesses it managed.

    requires: schema editable; the node carries a mask.
    ensures:  the mask and its managed data accesses are gone; all rules hold.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    form = candidate.forms.get(node_id)
    if form is None:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message=f"node '{node_id}' has no input mask",
                )
            ]
        )
    managed = {f.element_id for f in form.fields}
    candidate.data_accesses = [
        a
        for a in candidate.data_accesses
        if not (a.node_id == node_id and a.element_id in managed)
    ]
    del candidate.forms[node_id]
    return raise_if_invalid(candidate)


def register_connector(
    schema: ProcessSchema,
    name: str,
    kind: ConnectorKind,
    *,
    connector_id: str | None = None,
) -> ProcessSchema:
    """Register a data connector in the schema's connector registry (Section 9).

    requires: schema editable; connector id is unique.
    ensures:  the connector is available for external data bindings (C1).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    cid = connector_id or _new_id("connector")
    if cid in candidate.connectors:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"connector '{cid}' already exists")]
        )
    candidate.connectors[cid] = ConnectorDescriptor(id=cid, name=name, kind=kind)
    return raise_if_invalid(candidate)


def bind_external_data(
    schema: ProcessSchema,
    element_id: str,
    *,
    connector_id: str,
    entity: str,
    key_element_id: str,
) -> ProcessSchema:
    """Turn a data element into an EXTERNAL element resolved via a connector.

    requires: schema editable; the element exists.
    ensures:  the element's source is EXTERNAL and the connector rules C1-C3
              hold (registered connector, INSTANCE key element, non-empty
              entity).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    element = candidate.data_elements.get(element_id)
    if element is None:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"data element '{element_id}' does not exist")]
        )
    element.source = DataSourceKind.EXTERNAL
    element.select = None
    element.write = None
    element.external = ExternalBinding(
        connector_id=connector_id,
        entity=entity,
        key_element_id=key_element_id,
    )
    return raise_if_invalid(candidate)


def bind_sql_select(
    schema: ProcessSchema,
    element_id: str,
    *,
    connector_id: str,
    entity: str,
    column: str,
    column_type: DataType,
    aggregate: AggregateKind = AggregateKind.NONE,
    filters: list[QueryFilter] | None = None,
    cardinality: Cardinality = Cardinality.KEY_UNIQUE,
    order_by: list[OrderBy] | None = None,
    unique_column: str = "",
) -> ProcessSchema:
    """Bind a data element to a structured, scalar SQL select (concept §4, Q1).

    Turns the element into an EXTERNAL element whose value is a single, typed
    scalar compiled from a :class:`SqlSelectBinding` (never free-form SQL).

    requires: schema editable; the element exists.
    ensures:  the element's source is EXTERNAL with a scalar select binding and
              the scalar-query rules C4-C6 hold -- the projection's result type
              matches the element (C4), every filter is well-formed, type-
              conformant and supplied before the element is read (C5), and the
              select yields at most one row (C6). Otherwise the operation is
              rejected and the schema stays unchanged.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    element = candidate.data_elements.get(element_id)
    if element is None:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"data element '{element_id}' does not exist")]
        )
    element.source = DataSourceKind.EXTERNAL
    element.external = None
    element.write = None
    element.select = SqlSelectBinding(
        connector_id=connector_id,
        entity=entity,
        column=column,
        column_type=column_type,
        aggregate=aggregate,
        filters=list(filters or []),
        cardinality=cardinality,
        order_by=list(order_by or []),
        unique_column=unique_column,
    )
    return raise_if_invalid(candidate)


def bind_sql_write(
    schema: ProcessSchema,
    element_id: str,
    *,
    connector_id: str,
    entity: str,
    column: str,
    column_type: DataType,
    filters: list[QueryFilter] | None = None,
    unique_column: str = "",
) -> ProcessSchema:
    """Bind a data element to a structured scalar SQL write-back (concept §7, Q4).

    Turns the element into an EXTERNAL element whose produced scalar is written
    back via a parameterized ``UPDATE`` (never free-form SQL).

    requires: schema editable; the element exists.
    ensures:  the element's source is EXTERNAL with a scalar write binding and
              the scalar-write rules C7-C9 hold -- the target column type matches
              the element (C7), every filter is well-formed, type-conformant and
              supplied before the element is written (C8), and the write targets
              exactly one row (C9). Otherwise the operation is rejected and the
              schema stays unchanged.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    element = candidate.data_elements.get(element_id)
    if element is None:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"data element '{element_id}' does not exist")]
        )
    element.source = DataSourceKind.EXTERNAL
    element.external = None
    element.select = None
    element.write = SqlWriteBinding(
        connector_id=connector_id,
        entity=entity,
        column=column,
        column_type=column_type,
        filters=list(filters or []),
        unique_column=unique_column,
    )
    return raise_if_invalid(candidate)


def add_role(schema: ProcessSchema, name: str, role_id: str | None = None) -> ProcessSchema:
    """Add an organisational role to the schema's org model."""

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    _require_local_org(candidate)
    rid = role_id or _new_id("role")
    if rid in candidate.org_model.roles:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"role '{rid}' already exists")]
        )
    candidate.org_model.roles[rid] = Role(id=rid, name=name)
    return raise_if_invalid(candidate)


def add_org_unit(
    schema: ProcessSchema,
    name: str,
    parent_id: str | None = None,
    org_unit_id: str | None = None,
    manager_id: str | None = None,
) -> ProcessSchema:
    """Add an organisational unit (optionally below an existing parent unit).

    ``manager_id`` (optional) names the supervisor agent; it must reference an
    existing agent (checked via Z1).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    _require_local_org(candidate)
    uid = org_unit_id or _new_id("unit")
    if uid in candidate.org_model.org_units:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"org unit '{uid}' already exists")]
        )
    if parent_id is not None and parent_id not in candidate.org_model.org_units:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"parent org unit '{parent_id}' does not exist")]
        )
    candidate.org_model.org_units[uid] = OrgUnit(
        id=uid, name=name, parent_id=parent_id, manager_id=manager_id
    )
    return raise_if_invalid(candidate)


def add_agent(
    schema: ProcessSchema,
    name: str,
    role_ids: list[str] | None = None,
    org_unit_id: str | None = None,
    agent_id: str | None = None,
    deputy_id: str | None = None,
    email: str | None = None,
) -> ProcessSchema:
    """Add an agent (actor) and link it to existing roles / an org unit.

    ``deputy_id`` (optional) names a stand-in agent; it must reference an
    existing agent and cannot be the agent itself (checked via Z1). ``email``
    (optional) is the agent's personal mailbox for e-mail notifications; a
    malformed address is rejected (N1).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    _require_local_org(candidate)
    aid = agent_id or _new_id("agent")
    if aid in candidate.org_model.agents:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"agent '{aid}' already exists")]
        )
    roles = role_ids or []
    for role_id in roles:
        if role_id not in candidate.org_model.roles:
            raise CorrectnessError(
                [ValidationFinding(rule="OP", message=f"role '{role_id}' does not exist")]
            )
    if org_unit_id is not None and org_unit_id not in candidate.org_model.org_units:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"org unit '{org_unit_id}' does not exist")]
        )
    candidate.org_model.agents[aid] = Agent(
        id=aid,
        name=name,
        role_ids=roles,
        org_unit_id=org_unit_id,
        deputy_id=deputy_id,
        email=email,
    )
    return raise_if_invalid(candidate)


def set_org_unit_manager(
    schema: ProcessSchema, org_unit_id: str, manager_id: str | None
) -> ProcessSchema:
    """Set (or clear with ``None``) the supervisor of an org unit.

    Manager and deputy assignments are org master data, not process structure;
    they may therefore be changed on a RELEASED schema too, taking immediate
    effect for running instances. The result is still validated (Z1).
    """

    candidate = schema.model_copy(deep=True)
    _require_local_org(candidate)
    unit = candidate.org_model.org_units.get(org_unit_id)
    if unit is None:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"org unit '{org_unit_id}' does not exist")]
        )
    unit.manager_id = manager_id
    return raise_if_invalid(candidate)


def set_role_mailbox(
    schema: ProcessSchema, role_id: str, mailbox: str | None
) -> ProcessSchema:
    """Set (or clear with ``None``) a role's shared group mailbox (rule group N).

    Addresses are org master data (not process structure), so this is allowed on
    a RELEASED schema too and takes immediate effect. A malformed address is
    rejected (N1); removing a mailbox a group notification still needs is
    rejected (N3), so a released process never loses a required address.
    """

    candidate = schema.model_copy(deep=True)
    _require_local_org(candidate)
    role = candidate.org_model.roles.get(role_id)
    if role is None:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"role '{role_id}' does not exist")]
        )
    role.mailbox = mailbox
    return raise_if_invalid(candidate)


def set_unit_mailbox(
    schema: ProcessSchema, org_unit_id: str, mailbox: str | None
) -> ProcessSchema:
    """Set (or clear with ``None``) an org unit's department mailbox (rule group N).

    Same master-data semantics as :func:`set_role_mailbox`: allowed on a RELEASED
    schema, well-formedness checked (N1), and a mailbox a group notification
    still needs cannot be removed (N3).
    """

    candidate = schema.model_copy(deep=True)
    _require_local_org(candidate)
    unit = candidate.org_model.org_units.get(org_unit_id)
    if unit is None:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"org unit '{org_unit_id}' does not exist")]
        )
    unit.mailbox = mailbox
    return raise_if_invalid(candidate)


def set_org_unit_parent(
    schema: ProcessSchema, org_unit_id: str, parent_id: str | None
) -> ProcessSchema:
    """Move an org unit below another parent (or to the top with ``None``).

    The organisational hierarchy is master data that mirrors reality, so a
    re-org may be applied to a RELEASED schema too (it takes immediate effect
    for running instances via recursive ORG_UNIT resolution). The result is
    validated (Z1). A move that would create a cycle -- making the unit its own
    ancestor -- is rejected, since the tree must stay acyclic.
    """

    candidate = schema.model_copy(deep=True)
    _require_local_org(candidate)
    units = candidate.org_model.org_units
    unit = units.get(org_unit_id)
    if unit is None:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"org unit '{org_unit_id}' does not exist")]
        )
    if parent_id is not None:
        if parent_id not in units:
            raise CorrectnessError(
                [
                    ValidationFinding(
                        rule="OP",
                        message=f"parent org unit '{parent_id}' does not exist",
                    )
                ]
            )
        if parent_id == org_unit_id:
            raise CorrectnessError(
                [ValidationFinding(rule="OP", message="an org unit cannot be its own parent")]
            )
        # Walk up from the prospective parent; hitting the unit means a cycle.
        cursor: str | None = parent_id
        seen: set[str] = set()
        while cursor is not None and cursor not in seen:
            if cursor == org_unit_id:
                raise CorrectnessError(
                    [
                        ValidationFinding(
                            rule="OP",
                            message="move would create a cycle in the org hierarchy",
                        )
                    ]
                )
            seen.add(cursor)
            cursor = units[cursor].parent_id
    unit.parent_id = parent_id
    return raise_if_invalid(candidate)


def set_agent_deputy(
    schema: ProcessSchema, agent_id: str, deputy_id: str | None
) -> ProcessSchema:
    """Set (or clear with ``None``) an agent's deputy (Vertreter).

    A person defines their own stand-in; this is org master data and may be
    changed on a RELEASED schema too. The result is validated (Z1: the deputy
    must exist and differ from the agent).
    """

    candidate = schema.model_copy(deep=True)
    _require_local_org(candidate)
    agent = candidate.org_model.agents.get(agent_id)
    if agent is None:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"agent '{agent_id}' does not exist")]
        )
    agent.deputy_id = deputy_id
    return raise_if_invalid(candidate)


def link_org_model(schema: ProcessSchema, org_model_id: str, org: OrgModel) -> ProcessSchema:
    """Link a schema to a shared org model (its master data becomes ``org``).

    The shared model *org* is hydrated into the schema's embedded ``org_model``
    so the result can be validated (validate-before-commit); any previously
    embedded local org master data is replaced. Linking is a structural change
    to how staffing is resolved, so the schema must be in ENTWURF (R0). The
    referenced staff rules must still resolve against the shared model, else
    the result is rejected (Z1-Z4).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    candidate.org_model_id = org_model_id
    candidate.org_model = org.model_copy(deep=True)
    return raise_if_invalid(candidate)


def unlink_org_model(schema: ProcessSchema) -> ProcessSchema:
    """Detach a schema from its shared org model, keeping a local copy.

    The currently hydrated org master data is retained as the schema's own
    embedded ``org_model`` so existing staff rules keep resolving; afterwards
    the org can be edited locally again. Requires ENTWURF (R0).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    candidate.org_model_id = None
    candidate.org_model = candidate.org_model.model_copy(
        deep=True, update={"id": None, "name": ""}
    )
    return raise_if_invalid(candidate)


class _KeepSentinel:
    """Marker meaning 'leave this field unchanged' in a partial update."""


KEEP = _KeepSentinel()


def update_agent(
    schema: ProcessSchema,
    agent_id: str,
    *,
    name: str | None = None,
    role_ids: list[str] | None = None,
    org_unit_id: str | None | _KeepSentinel = KEEP,
    email: str | None | _KeepSentinel = KEEP,
) -> ProcessSchema:
    """Update an existing agent's master data (name, roles, org unit, e-mail).

    Only the provided fields change: ``name`` / ``role_ids`` left at ``None``
    keep their current value, and ``org_unit_id`` / ``email`` default to the
    ``KEEP`` sentinel (pass ``None`` explicitly to detach the org unit or clear
    the address). The agent id and deputy are untouched -- use
    ``set_agent_deputy`` for the latter. Editing the actor catalogue is a
    structural change, so the schema must be in ENTWURF (R0); referenced roles /
    the org unit must exist and the result is validated (Z1-Z4 and N1/N3), e.g.
    removing a role still required by a staff rule is rejected, and clearing an
    e-mail address still needed by a per-agent notification is rejected too.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    _require_local_org(candidate)
    agent = candidate.org_model.agents.get(agent_id)
    if agent is None:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"agent '{agent_id}' does not exist")]
        )
    if name is not None:
        agent.name = name
    if role_ids is not None:
        for role_id in role_ids:
            if role_id not in candidate.org_model.roles:
                raise CorrectnessError(
                    [ValidationFinding(rule="OP", message=f"role '{role_id}' does not exist")]
                )
        agent.role_ids = role_ids
    if not isinstance(org_unit_id, _KeepSentinel):
        if org_unit_id is not None and org_unit_id not in candidate.org_model.org_units:
            raise CorrectnessError(
                [ValidationFinding(rule="OP", message=f"org unit '{org_unit_id}' does not exist")]
            )
        agent.org_unit_id = org_unit_id
    if not isinstance(email, _KeepSentinel):
        agent.email = email
    return raise_if_invalid(candidate)


def add_activity_template(
    schema: ProcessSchema,
    name: str,
    executor: ExecutorKind,
    *,
    inputs: list[TemplateParameter] | None = None,
    outputs: list[TemplateParameter] | None = None,
    template_id: str | None = None,
) -> ProcessSchema:
    """Add a reusable activity template to the repository (Section 6).

    requires: schema editable; ``template_id`` (if given) is not already used.
    ensures:  the template is stored and the schema stays correct.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    tid = template_id or _new_id("template")
    if tid in candidate.activity_templates:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    message=f"activity template '{tid}' already exists",
                )
            ]
        )
    candidate.activity_templates[tid] = ActivityTemplate(
        id=tid,
        name=name,
        executor=executor,
        inputs=list(inputs or []),
        outputs=list(outputs or []),
    )
    return raise_if_invalid(candidate)


def assign_service(
    schema: ProcessSchema,
    node_id: str,
    name: str,
    *,
    automatic: bool = False,
    template_id: str | None = None,
    parameter_mapping: dict[str, str] | None = None,
) -> ProcessSchema:
    """Bind an executing service (ActivityTemplate) to an ACTIVITY node.

    requires: schema editable; node exists and is an ACTIVITY; if
              ``template_id`` is given it must exist in the repository.
    ensures:  the service binding is set and the resource/repository rules
              Z1-Z4 and A1-A3 hold. When a template is referenced, ``automatic``
              is derived from its executor so the binding is consistent (A2).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type is not NodeType.ACTIVITY:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="service can only be bound to ACTIVITY nodes",
                )
            ]
        )
    if template_id is not None:
        template = candidate.activity_templates.get(template_id)
        if template is None:
            raise CorrectnessError(
                [
                    ValidationFinding(
                        rule="OP",
                        node_id=node_id,
                        message=f"unknown activity template '{template_id}'",
                    )
                ]
            )
        automatic = template.is_automatic
    candidate.service_bindings[node_id] = ServiceBinding(
        node_id=node_id,
        name=name,
        automatic=automatic,
        template_id=template_id,
        parameter_mapping=dict(parameter_mapping or {}),
    )
    return raise_if_invalid(candidate)


def unassign_service(schema: ProcessSchema, node_id: str) -> ProcessSchema:
    """Remove the executing service (and any automation config) from a node.

    Inverse of :func:`assign_service`. A step without a service is well-formed in
    the draft -- the "every step has an executable service" requirement B1 is
    only enforced at release -- so the removal is validated like every other
    change (No-Bypass) and rolled back should it ever violate a rule. Because the
    automation fields (E11) live on the same :class:`ServiceBinding`, they are
    removed together with it, keeping the integration rules I1-I4 satisfied.

    requires: schema editable; the node carries a service binding.
    ensures:  the binding is removed and the full rule set still holds.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    if node_id not in candidate.service_bindings:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message=f"node '{node_id}' has no service binding",
                )
            ]
        )
    del candidate.service_bindings[node_id]
    return raise_if_invalid(candidate)


def assign_staff_rule(
    schema: ProcessSchema, node_id: str, rule: StaffRule
) -> ProcessSchema:
    """Assign a staff-assignment rule (BZR) to an ACTIVITY node.

    requires: schema editable; node exists and is an ACTIVITY.
    ensures:  the rule is stored and the resource rules Z1-Z4 hold (e.g. an
              unknown role yields Z1, an unsatisfiable rule yields Z2, an
              invalid NodePerformingAgent back-reference yields Z3).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type is not NodeType.ACTIVITY:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="staff rule can only be assigned to ACTIVITY nodes",
                )
            ]
        )
    candidate.staff_rules[node_id] = rule
    return raise_if_invalid(candidate)


def clear_staff_rule(schema: ProcessSchema, node_id: str) -> ProcessSchema:
    """Remove the staff-assignment rule (BZR) from an ACTIVITY node.

    Inverse of :func:`assign_staff_rule`. A node without a rule is well-formed in
    the draft (the "every interactive step has a worker" requirement B2 is only
    enforced at release), so the removal is validated like any other change and
    rolled back should it ever violate a rule -- No-Bypass, same path as every
    other mutation.

    requires: schema editable; the node carries a staff rule.
    ensures:  the rule is removed and the full rule set still holds.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    if node_id not in candidate.staff_rules:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message=f"node '{node_id}' has no staff rule",
                )
            ]
        )
    del candidate.staff_rules[node_id]
    return raise_if_invalid(candidate)


def release(schema: ProcessSchema, resolver: SchemaResolver | None = None) -> ProcessSchema:
    """Release the schema (lifecycle transition ENTWURF/REVIEW -> RELEASED).

    requires: structural correctness (Stufe A: K1-K3 hold).
    ensures:  schema becomes RELEASED (immutable for further edits).

    ``resolver`` enables the cross-schema composition checks (H1: a SUBPROCESS
    must reference a RELEASED target).

    Beyond structural correctness this is the **Stufe-B gate** (concept §3.4):
    :func:`~procworks.validator.check_executable` additionally requires every
    interactive step to carry a staff rule (B2), because a step without one is
    activated at runtime but shows up in no worklist. A draft may sit in that
    state as long as it likes -- the bar only applies at release, which is the
    moment the schema becomes instantiable. This is the one place the two levels
    meet: Stufe A is re-checked (as after every operation) and Stufe B on top.
    """

    if schema.lifecycle_state not in (LifecycleState.ENTWURF, LifecycleState.REVIEW):
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="LC",
                    message=f"cannot release from state {schema.lifecycle_state.value}",
                )
            ]
        )
    raise_if_invalid(schema, resolver)
    executable_findings = check_executable(schema)
    if executable_findings:
        raise CorrectnessError(executable_findings)
    released = schema.model_copy(deep=True)
    released.lifecycle_state = LifecycleState.RELEASED
    return released


def new_revision(schema: ProcessSchema, *, new_schema_id: str | None = None) -> ProcessSchema:
    """Derive an editable next revision (version + 1) of a RELEASED schema.

    The copy keeps all node/edge/data element ids so a later instance migration
    (M2/M3) can match the already-executed region by id. It starts in ENTWURF
    and gets a fresh schema id (the single-version store keys by id, so the new
    revision is stored alongside its predecessor).

    requires: schema is RELEASED.
    ensures:  returns an ENTWURF copy with ``version`` incremented.
    """

    if schema.lifecycle_state is not LifecycleState.RELEASED:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="LC",
                    message=(
                        f"can only revise a RELEASED schema, not {schema.lifecycle_state.value}"
                    ),
                )
            ]
        )
    revision = schema.model_copy(deep=True)
    revision.id = new_schema_id or _new_id("schema")
    revision.version = schema.version + 1
    revision.lifecycle_state = LifecycleState.ENTWURF
    return raise_if_invalid(revision)


def snapshot_for_template(schema: ProcessSchema, *, snapshot_id: str) -> ProcessSchema:
    """Return a self-contained, portable copy of ``schema`` for a template.

    The blueprint stored inside a :class:`ProcessTemplate` must not depend on
    external state: the (already hydrated) organisation master data is embedded
    and ``org_model_id`` is cleared, and the copy is reset to a clean draft
    (``ENTWURF``/``version == 1``). Validated before it is returned, so a
    template can never carry an incorrect blueprint (No-Bypass).

    requires: ``schema`` is hydrated (its ``org_model`` reflects the live org
              when it references a shared one -- the API boundary hydrates on
              read); the schema is correct.
    ensures:  returns a deep copy with ``id == snapshot_id``, no shared-org link
              and embedded org master data, ``ENTWURF``/``version == 1``.
    """

    candidate = schema.model_copy(deep=True)
    candidate.id = snapshot_id
    candidate.org_model_id = None
    candidate.lifecycle_state = LifecycleState.ENTWURF
    candidate.version = 1
    return raise_if_invalid(candidate)


def save_as_template(
    schema: ProcessSchema,
    *,
    name: str,
    description: str = "",
    category: str = "",
    template_id: str | None = None,
    origin: TemplateOrigin = TemplateOrigin.USER,
) -> ProcessTemplate:
    """Capture ``schema`` as a reusable :class:`ProcessTemplate`.

    The template stores a self-contained snapshot of the schema (see
    :func:`snapshot_for_template`) plus catalogue metadata. Used both by the
    modeller-facing "save as template" endpoint (``origin`` USER) and by the
    built-in template library (``origin`` BUILTIN).

    requires: ``schema`` is a correct, hydrated schema.
    ensures:  returns a template whose embedded blueprint is validated; raises
              ``CorrectnessError`` otherwise (validate-before-commit).
    """

    tid = template_id or _new_id("tpl")
    snapshot = snapshot_for_template(schema, snapshot_id=tid)
    return ProcessTemplate(
        id=tid,
        name=name,
        description=description,
        category=category,
        origin=origin,
        blueprint=snapshot,
        created_at=datetime.now(UTC),
    )


def instantiate_template(
    template: ProcessTemplate,
    *,
    schema_id: str | None = None,
    name: str | None = None,
) -> ProcessSchema:
    """Create a fresh, editable draft schema from a template blueprint.

    Deep-copies the template's self-contained schema, assigns a new schema id
    and (optionally) a new name, forces a clean ``ENTWURF``/``version == 1``
    draft, and validates before returning (validate-before-commit). The result
    is an ordinary draft schema the modeller edits and releases like any other.

    requires: the template's blueprint is correct (guaranteed on save).
    ensures:  returns a new ``ENTWURF`` schema with a fresh id; raises
              ``CorrectnessError`` if the blueprint no longer validates.
    """

    candidate = template.blueprint.model_copy(deep=True)
    candidate.id = schema_id or _new_id("schema")
    candidate.name = name or template.name
    candidate.lifecycle_state = LifecycleState.ENTWURF
    candidate.version = 1
    return raise_if_invalid(candidate)


def insert_subprocess(
    schema: ProcessSchema,
    after_node_id: str,
    target_schema_id: str,
    target_version: int,
    *,
    label: str = "",
    input_mapping: dict[str, str] | None = None,
    output_mapping: dict[str, str] | None = None,
    resolver: SchemaResolver | None = None,
) -> ProcessSchema:
    """Insert a SUBPROCESS node sequentially and bind it to a target schema.

    requires: schema editable; anchor exists, is not END, has one outgoing
              edge.
    ensures:  new SUBPROCESS spliced in with a binding; K1-K3 and H1-H4 hold
              (the latter only fully when a ``resolver`` is provided).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    anchor = _require_node(candidate, after_node_id)
    if anchor.type is NodeType.END:
        raise CorrectnessError(
            [ValidationFinding(rule="OP", node_id=after_node_id, message="cannot insert after END")]
        )
    edge = _single_outgoing(candidate, after_node_id)
    successor_id = edge.target

    new_node = Node(id=_new_id("sub"), type=NodeType.SUBPROCESS, label=label)
    candidate.nodes[new_node.id] = new_node
    candidate.edges.remove(edge)
    candidate.edges.append(ControlEdge(source=after_node_id, target=new_node.id))
    candidate.edges.append(ControlEdge(source=new_node.id, target=successor_id))
    candidate.sub_process_bindings[new_node.id] = SubProcessBinding(
        node_id=new_node.id,
        target_schema_id=target_schema_id,
        target_version=target_version,
        input_mapping=input_mapping or {},
        output_mapping=output_mapping or {},
    )
    return raise_if_invalid(candidate, resolver)


def set_subprocess_mapping(
    schema: ProcessSchema,
    node_id: str,
    input_mapping: dict[str, str],
    output_mapping: dict[str, str],
    *,
    resolver: SchemaResolver | None = None,
) -> ProcessSchema:
    """Replace the input/output mapping of an existing sub-process binding."""

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    binding = candidate.sub_process_bindings.get(node_id)
    if binding is None:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message=f"node '{node_id}' has no sub-process binding",
                )
            ]
        )
    binding.input_mapping = dict(input_mapping)
    binding.output_mapping = dict(output_mapping)
    return raise_if_invalid(candidate, resolver)


def convert_activity_to_subprocess(
    schema: ProcessSchema,
    node_id: str,
    target_schema_id: str,
    target_version: int,
    *,
    input_mapping: dict[str, str] | None = None,
    output_mapping: dict[str, str] | None = None,
    resolver: SchemaResolver | None = None,
) -> ProcessSchema:
    """Turn an existing ACTIVITY into a SUBPROCESS bound to a target schema.

    The sub-process is developed independently (its own RELEASED schema) and
    reused here. The activity becomes an opaque black box that delegates to the
    child, so its activity-specific artefacts (data accesses, input mask, staff
    rule, service binding, priority, temporal constraint) are dropped; data now
    flows through the binding's input/output mapping.

    requires: schema editable (R0); node exists and is an ACTIVITY.
    ensures:  the node is a SUBPROCESS with a binding and the WHOLE model stays
              correct and runnable (H1-H4 incl. data-passing soundness, plus all
              structural/data rules) -- Correctness by Construction on binding.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type is not NodeType.ACTIVITY:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="only an ACTIVITY can be converted into a sub-process",
                )
            ]
        )
    node.type = NodeType.SUBPROCESS
    candidate.data_accesses = [a for a in candidate.data_accesses if a.node_id != node_id]
    candidate.forms.pop(node_id, None)
    candidate.staff_rules.pop(node_id, None)
    candidate.service_bindings.pop(node_id, None)
    candidate.node_priorities.pop(node_id, None)
    candidate.time_constraints.pop(node_id, None)
    candidate.sub_process_bindings[node_id] = SubProcessBinding(
        node_id=node_id,
        target_schema_id=target_schema_id,
        target_version=target_version,
        input_mapping=input_mapping or {},
        output_mapping=output_mapping or {},
    )
    return raise_if_invalid(candidate, resolver)


def set_subprocess_binding(
    schema: ProcessSchema,
    node_id: str,
    target_schema_id: str,
    target_version: int,
    *,
    input_mapping: dict[str, str] | None = None,
    output_mapping: dict[str, str] | None = None,
    resolver: SchemaResolver | None = None,
) -> ProcessSchema:
    """Re-point an existing SUBPROCESS node to a (different) target schema.

    Like :func:`convert_activity_to_subprocess`, the binding is only accepted if
    the resulting whole model stays correct and runnable (CbC).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type is not NodeType.SUBPROCESS:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="node is not a SUBPROCESS",
                )
            ]
        )
    candidate.sub_process_bindings[node_id] = SubProcessBinding(
        node_id=node_id,
        target_schema_id=target_schema_id,
        target_version=target_version,
        input_mapping=input_mapping or {},
        output_mapping=output_mapping or {},
    )
    return raise_if_invalid(candidate, resolver)


def set_library_subprocess(schema: ProcessSchema, flag: bool) -> ProcessSchema:
    """Mark or unmark this schema as a reusable sub-process for the library.

    The flag is pure catalogue metadata (it never affects validation), so it may
    be toggled in any lifecycle state -- in particular on a RELEASED schema,
    which is exactly when it becomes bindable elsewhere.
    """

    candidate = schema.model_copy(deep=True)
    candidate.is_library_subprocess = bool(flag)
    return candidate


def link_follow_up(
    schema: ProcessSchema,
    target_schema_id: str,
    *,
    target_version: int | None = None,
    trigger: FollowUpTrigger = FollowUpTrigger.ON_COMPLETE,
    condition: str | None = None,
    handover_mapping: dict[str, str] | None = None,
    mode: FollowUpMode = FollowUpMode.ASYNC,
    resolver: SchemaResolver | None = None,
    link_id: str | None = None,
) -> ProcessSchema:
    """Add a follow-up link to another process type (F1-F3)."""

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    candidate.follow_up_links.append(
        FollowUpLink(
            id=link_id or _new_id("followup"),
            target_schema_id=target_schema_id,
            target_version=target_version,
            trigger=trigger,
            condition=condition,
            handover_mapping=handover_mapping or {},
            mode=mode,
        )
    )
    return raise_if_invalid(candidate, resolver)


def unlink_follow_up(schema: ProcessSchema, link_id: str) -> ProcessSchema:
    """Remove a follow-up link by id."""

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    remaining = [link for link in candidate.follow_up_links if link.id != link_id]
    if len(remaining) == len(candidate.follow_up_links):
        raise CorrectnessError(
            [ValidationFinding(rule="OP", message=f"follow-up link '{link_id}' does not exist")]
        )
    candidate.follow_up_links = remaining
    return raise_if_invalid(candidate)


# --- analytical annotations (value class, priority, time; E3/E8/E5) -------


def set_value_class(
    schema: ProcessSchema, node_id: str, value_class: ValueClass | None
) -> ProcessSchema:
    """Annotate the value-adding classification of an activity (roadmap E3).

    requires: schema editable (R0); node exists and is an ACTIVITY or
              SUBPROCESS (only performing steps carry value).
    ensures:  the node's ``value_class`` is set (or cleared with ``None``); the
              schema stays correct -- the annotation has no structural weight.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type not in (NodeType.ACTIVITY, NodeType.SUBPROCESS):
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="only ACTIVITY or SUBPROCESS nodes carry a value class",
                )
            ]
        )
    node.value_class = value_class
    return raise_if_invalid(candidate)


def set_automation(
    schema: ProcessSchema,
    node_id: str,
    automation: AutomationKind,
    *,
    topic: str | None = None,
    endpoint_ref: str | None = None,
    retry_max: int | None = None,
    retry_backoff_ms: int | None = None,
    request_timeout_ms: int | None = None,
) -> ProcessSchema:
    """Configure how an automatic ACTIVITY is driven by an external tool (E11).

    requires: schema editable (R0); the node exists, is an ACTIVITY and already
              carries a service binding (bind a service first via
              ``assign_service``).
    ensures:  the binding's ``automation`` and its topic/endpoint/retry settings
              are set; for a non-``MANUAL_NONE`` kind ``automatic`` is forced
              True so the step is never interactive; the integration rules
              I1-I4 are re-checked, so an ill-formed or secret-bearing binding
              is rejected (Correctness by Construction extends to integration).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type is not NodeType.ACTIVITY:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="automation can only be set on ACTIVITY nodes",
                )
            ]
        )
    binding = candidate.service_bindings.get(node_id)
    if binding is None:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="bind a service before configuring its automation",
                )
            ]
        )
    binding.automation = automation
    binding.topic = topic
    binding.endpoint_ref = endpoint_ref
    if automation is not AutomationKind.MANUAL_NONE:
        binding.automatic = True
    if retry_max is not None:
        binding.retry_max = retry_max
    if retry_backoff_ms is not None:
        binding.retry_backoff_ms = retry_backoff_ms
    if request_timeout_ms is not None:
        binding.request_timeout_ms = request_timeout_ms
    return raise_if_invalid(candidate)


def set_node_priority(
    schema: ProcessSchema,
    node_id: str,
    priority: WorkItemPriority | None,
) -> ProcessSchema:
    """Set (or clear) the work-item priority of an interactive node (E8).

    requires: schema editable (R0); node exists and is an ACTIVITY or
              SUBPROCESS (the steps that produce work items).
    ensures:  ``node_priorities[node_id]`` is set or removed; structure is
              unaffected, so the schema stays correct.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type not in (NodeType.ACTIVITY, NodeType.SUBPROCESS):
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="only ACTIVITY or SUBPROCESS nodes carry a priority",
                )
            ]
        )
    if priority is None:
        candidate.node_priorities.pop(node_id, None)
    else:
        candidate.node_priorities[node_id] = priority
    return raise_if_invalid(candidate)


def set_mail_binding(
    schema: ProcessSchema,
    node_id: str,
    binding: MailBinding | None,
) -> ProcessSchema:
    """Set (or clear with ``None``) the modelled e-mail notification of an
    activity (rule group N).

    requires: schema editable (R0); node exists and is an ACTIVITY (only an
              interactive step has an assignee to notify).
    ensures:  ``mail_bindings[node_id]`` is set or removed; the mail rules
              N1-N4 are re-checked, so a notification can only be modelled when
              every possible recipient has an address and every template
              placeholder resolves (Correctness by Construction extends to the
              notification). A binding whose recipients are not fully addressable
              is rejected rather than silently dropping mails at runtime.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type is not NodeType.ACTIVITY:
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="only ACTIVITY nodes can carry a mail notification",
                )
            ]
        )
    if binding is None:
        candidate.mail_bindings.pop(node_id, None)
    else:
        candidate.mail_bindings[node_id] = binding
    return raise_if_invalid(candidate)


def set_time_constraint(
    schema: ProcessSchema,
    node_id: str,
    constraint: TimeConstraint | None,
) -> ProcessSchema:
    """Set (or clear) the temporal annotation of a node (roadmap E5).

    requires: schema editable (R0); node exists and is an ACTIVITY or
              SUBPROCESS (only performing steps consume time here).
    ensures:  ``time_constraints[node_id]`` is set or removed; the temporal
              rules T1/T2 are re-checked, so an inconsistent annotation (e.g. a
              negative duration or a critical path beyond the deadline) is
              rejected (Correctness by Construction extends to time).
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    node = _require_node(candidate, node_id)
    if node.type not in (NodeType.ACTIVITY, NodeType.SUBPROCESS):
        raise CorrectnessError(
            [
                ValidationFinding(
                    rule="OP",
                    node_id=node_id,
                    message="only ACTIVITY or SUBPROCESS nodes carry a time constraint",
                )
            ]
        )
    if constraint is None:
        candidate.time_constraints.pop(node_id, None)
    else:
        candidate.time_constraints[node_id] = constraint
    return raise_if_invalid(candidate)


def set_escalation_policy(
    schema: ProcessSchema,
    node_id: str,
    policy: EscalationPolicy | None,
) -> ProcessSchema:
    """Set (or clear with ``None``) a node's overdue reaction (T3/E9).

    The policy's wellformedness -- interactive carrier with a resolvable
    target time, strictly ascending stages, resolvable non-node-referencing
    targets -- is the validator's job (T3a-T3c, validate-before-commit): an
    undecidable escalation can never be stored.

    requires: schema editable (R0); node exists.
    ensures:  ``escalation_policies[node_id]`` is set or removed and the full
              rule catalogue (incl. T3 and the N3 coupling) holds.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    _require_node(candidate, node_id)
    if policy is None:
        candidate.escalation_policies.pop(node_id, None)
    else:
        candidate.escalation_policies[node_id] = policy.model_copy(deep=True)
    return raise_if_invalid(candidate)


def set_deadline(
    schema: ProcessSchema, deadline_seconds: float | None
) -> ProcessSchema:
    """Set (or clear) the hard deadline of the whole process (roadmap E5).

    requires: schema editable (R0).
    ensures:  ``deadline_seconds`` is set or cleared; T1/T2 are re-checked so a
              negative deadline or an over-long critical path is rejected.
    """

    candidate = schema.model_copy(deep=True)
    _require_editable(candidate)
    candidate.deadline_seconds = deadline_seconds
    return raise_if_invalid(candidate)
