# SPDX-License-Identifier: BUSL-1.1
"""Correctness Validator (Stufe A, structural rules K1-K3).

The validator is called *before* committing any change operation (validate-
before-commit). It returns precise, localized findings. Operations refuse to
commit a schema that produces any finding, so a persisted schema always
satisfies the structural correctness invariant.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence

from pydantic import BaseModel

from procworks.conditions import ConditionError, referenced_names
from procworks.model import (
    JOIN_TYPES,
    LOOP_TYPES,
    READ_MODES,
    SPLIT_JOIN_PAIR,
    SPLIT_TYPES,
    STAFF_COMBINATOR_KINDS,
    STAFF_LEAF_KINDS,
    STAFF_NODE_REF_KINDS,
    WRITE_MODES,
    ActivityTemplate,
    AggregateKind,
    AutomationKind,
    Cardinality,
    DataElement,
    DataSourceKind,
    DataType,
    EdgeType,
    EscalationKind,
    FilterOperator,
    FollowUpTrigger,
    LifecycleState,
    MailBinding,
    MailRecipientMode,
    NodeType,
    OrgModel,
    PartitionCell,
    ProcessSchema,
    ServiceBinding,
    SqlSelectBinding,
    StaffRule,
    StaffRuleKind,
    SubProcessBinding,
    WidgetKind,
    XorDecisionKind,
    aggregate_result_type,
    block_join,
    discriminator_kind,
    is_valid_email,
    loop_block,
    template_placeholders,
    widget_matches_type,
)
from procworks.worklist_priority import target_seconds

#: Resolves a (schema id, version) reference to a schema, or ``None`` if the
#: version is ``None`` it resolves the latest known schema for that id. Used by
#: the cross-schema composition rules (H1-H4, F1-F3).
SchemaResolver = Callable[[str, "int | None"], "ProcessSchema | None"]


class ValidationFinding(BaseModel):
    """A single, localized correctness violation."""

    rule: str
    message: str
    node_id: str | None = None


class CorrectnessError(Exception):
    """Raised when an operation would produce an incorrect schema."""

    def __init__(self, findings: list[ValidationFinding]) -> None:
        self.findings = findings
        super().__init__("; ".join(f"[{f.rule}] {f.message}" for f in findings))


def validate(
    schema: ProcessSchema, resolver: SchemaResolver | None = None
) -> list[ValidationFinding]:
    """Run structural rules K1-K3/K6/K7, data-flow D1-D4, resource rules Z1-Z4,
    activity-repository rules A1-A3, composition rules H1-H4/F1-F4, the
    integration rules I1-I4, the temporal rules T1-T2 and the input-mask rules
    U1-U3.

    ``resolver`` enables the cross-schema composition checks (target must be
    RELEASED, type-conformant mappings, acyclic hierarchy). Without it only the
    local well-formedness of sub-process/follow-up references is checked.

    The integration rules I1-I4 are silent unless a service binding declares an
    ``automation`` other than ``MANUAL_NONE``; the temporal rules T1-T2 are
    silent unless the schema carries temporal annotations. Both groups never
    affect integration-free / time-free models.

    Returns all findings (an empty list means the schema is correct).
    """

    findings: list[ValidationFinding] = []
    findings += _check_k2_endpoints_and_degrees(schema)
    findings += _check_k1_gateways(schema)
    findings += _check_k6_loops(schema)
    findings += _check_k4_sync_edges(schema)
    findings += _check_k7_xor_decisions(schema)
    findings += _check_k3_reachability(schema)
    findings += _check_data_flow(schema)
    findings += _check_forms(schema)
    findings += _check_connectors(schema)
    findings += _check_scalar_queries(schema)
    findings += _check_scalar_writes(schema)
    findings += _check_resources(schema)
    findings += _check_integration(schema)
    findings += _check_composition(schema, resolver)
    findings += _check_temporal(schema)
    findings += _check_t3_escalations(schema)
    findings += _check_mail(schema)
    return findings


def raise_if_invalid(
    schema: ProcessSchema, resolver: SchemaResolver | None = None
) -> ProcessSchema:
    """Return the schema if correct, otherwise raise CorrectnessError."""

    findings = validate(schema, resolver)
    if findings:
        raise CorrectnessError(findings)
    return schema


# --- B2: release readiness / executability (Stufe B) ---------------------


def check_executable(schema: ProcessSchema) -> list[ValidationFinding]:
    """Stufe-B check: is the schema not merely *correct* but also *runnable*?

    Deliberately **not** part of :func:`validate`. Stufe A (K/D/Z/…) holds after
    every single operation, so a half-finished draft stays editable; Stufe B is
    the additional bar a schema must clear to be **released** (concept §1.1.1,
    §3.4). Calling it from ``validate`` would make an incomplete draft
    unmodellable, which is exactly the "Verbotsmodell" the architecture rejects.

    Implemented rule:

    * **B2 -- Bearbeiterzuordnung.** Every *interactive* ACTIVITY carries a staff
      rule (BZR). Without one the step is activated at runtime but appears in no
      worklist (:func:`procworks.assignment.open_tasks` skips nodes without a
      rule), so the instance cannot be driven forward by the people meant to work
      it. An **automatic** step is exempt -- rule Z4 even forbids a BZR there.

    Not enforced here, on purpose:

    * **B1 -- Dienstzuordnung** as literally worded in the concept ("every
      activity node is linked to an executable ActivityTemplate") does not match
      how the product is actually used: an interactive step with an input mask
      needs no service, and *none* of the shipped example processes or built-in
      templates binds one. Enforcing it would reject the product's own corpus.
      The concept text is the outdated part; see §3.4.
    * **B3 -- Vollständige Datenbindung** is already guaranteed by Stufe A: D1
      supplies every mandatory input on every path and K7 makes each branch
      partition total, both checked on every commit. A separate gate would only
      re-assert what cannot be violated.

    Returns an empty list when the schema is releasable.
    """

    findings: list[ValidationFinding] = []
    for node in schema.nodes.values():
        if node.type is not NodeType.ACTIVITY:
            continue
        binding = schema.service_bindings.get(node.id)
        if binding is not None and binding.automatic:
            continue  # automatic step: no performer needed (and Z4 forbids one)
        if node.id in schema.staff_rules:
            continue
        findings.append(
            ValidationFinding(
                rule="B2",
                node_id=node.id,
                message=(
                    f"interactive step '{node.label or node.id}' has no staff rule "
                    f"(BZR); it would be activated but appear in nobody's worklist"
                ),
            )
        )
    return findings


# --- K2: single start/end, well-formed in/out degrees --------------------


def _check_k2_endpoints_and_degrees(schema: ProcessSchema) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []

    starts = [n for n in schema.nodes.values() if n.type is NodeType.START]
    ends = [n for n in schema.nodes.values() if n.type is NodeType.END]
    if len(starts) != 1:
        findings.append(
            ValidationFinding(rule="K2", message=f"expected exactly one START, found {len(starts)}")
        )
    if len(ends) != 1:
        findings.append(
            ValidationFinding(rule="K2", message=f"expected exactly one END, found {len(ends)}")
        )

    for node in schema.nodes.values():
        ind = len(schema.incoming(node.id))
        outd = len(schema.outgoing(node.id))
        if node.type is NodeType.START:
            if ind != 0 or outd != 1:
                findings.append(_deg(node.id, "START must have in=0, out=1", ind, outd))
        elif node.type is NodeType.END:
            if ind != 1 or outd != 0:
                findings.append(_deg(node.id, "END must have in=1, out=0", ind, outd))
        elif node.type is NodeType.ACTIVITY:
            if ind != 1 or outd != 1:
                findings.append(_deg(node.id, "ACTIVITY must have in=1, out=1", ind, outd))
        elif node.type is NodeType.SUBPROCESS:
            if ind != 1 or outd != 1:
                findings.append(_deg(node.id, "SUBPROCESS must have in=1, out=1", ind, outd))
        elif node.type in SPLIT_TYPES:
            if ind != 1 or outd < 2:
                msg = f"{node.type.value} must have in=1, out>=2"
                findings.append(_deg(node.id, msg, ind, outd))
        elif node.type in JOIN_TYPES:
            if ind < 2 or outd != 1:
                msg = f"{node.type.value} must have in>=2, out=1"
                findings.append(_deg(node.id, msg, ind, outd))
        elif node.type in LOOP_TYPES:
            # Loop delimiters are serial nodes on the stored (acyclic) graph;
            # the back edge is implicit in the K6 pairing, never a stored edge.
            if ind != 1 or outd != 1:
                msg = f"{node.type.value} must have in=1, out=1"
                findings.append(_deg(node.id, msg, ind, outd))

    return findings


def _deg(node_id: str, msg: str, ind: int, outd: int) -> ValidationFinding:
    return ValidationFinding(rule="K2", node_id=node_id, message=f"{msg} (in={ind}, out={outd})")


# --- K1: balanced, matching gateways -------------------------------------


def _check_k1_gateways(schema: ProcessSchema) -> list[ValidationFinding]:
    """K1: gateways are balanced **and** form properly nested, same-type blocks.

    Two stages, because they fail differently:

    1. **Counting.** Per gateway kind the number of splits must equal the number
       of joins. Catches the coarse cases (a split with no join at all).
    2. **Pairing/nesting.** Every split must be closed by *one* join, reached at
       nesting depth 0 on **all** of its branches, of the matching type, and no
       join may close two splits. This is what makes the graph block-structured
       in the ADEPT sense -- counting alone does not: two crossed blocks
       (``s1 -> s2 -> ... -> j1 -> j2``) have perfectly balanced counts.

    Stage 2 is the guarantee the rest of the system *relies* on and therefore may
    not merely assume: the engine's join semantics (a join is skipped when an
    incoming branch was deselected) are sound only on properly nested blocks. A
    crossed block lets an XOR-deselected branch skip an AND join, which cascades
    ``SKIPPED`` to the END node -- the instance can then never complete and never
    appears in a worklist again (K5 "option to complete" and "proper completion"
    both lost). The same holds for the must-write analysis behind D1, which
    unions at an AND join and intersects elsewhere, and for the K4 branch
    relation. Stage 2 is why those assumptions hold.

    Stage 2 is skipped while stage 1 or the K2 degree rules already report
    findings -- on a graph whose degrees are broken the walk would only add
    noise on top of a diagnosis the user already has.
    """

    findings: list[ValidationFinding] = []
    for split_type, join_type in SPLIT_JOIN_PAIR.items():
        n_splits = sum(1 for n in schema.nodes.values() if n.type is split_type)
        n_joins = sum(1 for n in schema.nodes.values() if n.type is join_type)
        if n_splits != n_joins:
            findings.append(
                ValidationFinding(
                    rule="K1",
                    message=(
                        f"unbalanced gateways: {n_splits} x {split_type.value} "
                        f"vs {n_joins} x {join_type.value}"
                    ),
                )
            )
    if findings or _check_k2_endpoints_and_degrees(schema):
        return findings

    claimed_by: dict[str, str] = {}
    for node in schema.nodes.values():
        if node.type not in SPLIT_TYPES:
            continue
        try:
            join_id, _ = block_join(schema, node.id)
        except ValueError as exc:
            findings.append(
                ValidationFinding(rule="K1", node_id=node.id, message=str(exc))
            )
            continue
        expected = SPLIT_JOIN_PAIR[node.type]
        actual = schema.nodes[join_id].type
        if actual is not expected:
            findings.append(
                ValidationFinding(
                    rule="K1",
                    node_id=node.id,
                    message=(
                        f"{node.type.value} '{node.id}' is closed by "
                        f"{actual.value} '{join_id}', expected {expected.value}"
                    ),
                )
            )
        if join_id in claimed_by:
            findings.append(
                ValidationFinding(
                    rule="K1",
                    node_id=join_id,
                    message=(
                        f"join '{join_id}' closes two splits "
                        f"('{claimed_by[join_id]}' and '{node.id}')"
                    ),
                )
            )
            continue
        claimed_by[join_id] = node.id
    # No "join closes no split" check needed: the counts balance per kind and
    # every split claims a distinct join, so an unclaimed join is impossible
    # unless one of the findings above already fired.
    return findings


# --- K6: structured REPEAT-UNTIL loops ------------------------------------


def _check_k6_loops(schema: ProcessSchema) -> list[ValidationFinding]:
    """K6: loops are properly paired and decidable per iteration.

    Fully additive -- a schema without LOOP nodes produces no findings. The
    sub-rules (Schleifen-Konzept §4):

    - K6a: LOOP_START/LOOP_END occur only as properly nested pairs.
    - K6b: every LOOP_END carries exactly one ``LoopDecision`` over an
      existing BOOLEAN INSTANCE element; no stale decisions elsewhere.
    - K6c: the discriminator is mandatorily written on **every** path through
      the loop body, so each iteration decides on fresh data (D1-analog,
      block-local must-write analysis).
    - K6d: the body is non-empty (an empty loop is meaningless).

    The former stage-S1 restriction K6e (no SUBPROCESS / automatic activity in
    the body) was lifted in stage S3: the loop reset clears the body's
    child-instance links so a repeated SUBPROCESS spawns a fresh child, and the
    external-task machinery is iteration-safe by construction (an *open* task
    keeps its step uncompleted, so no open task can ever cross a reset, while a
    COMPLETED one does not block re-materialisation).
    """

    findings: list[ValidationFinding] = []

    def fail(message: str, node_id: str | None = None) -> None:
        findings.append(ValidationFinding(rule="K6", node_id=node_id, message=message))

    starts = [n.id for n in schema.nodes.values() if n.type is NodeType.LOOP_START]
    ends = {n.id for n in schema.nodes.values() if n.type is NodeType.LOOP_END}
    if not starts and not ends and not schema.loop_decisions:
        return findings

    for node_id in schema.loop_decisions:
        node = schema.nodes.get(node_id)
        if node is None or node.type is not NodeType.LOOP_END:
            fail("loop decision references a node that is not a LOOP_END", node_id)

    if len(starts) != len(ends):
        fail(f"unbalanced loops: {len(starts)} x LOOP_START vs {len(ends)} x LOOP_END")

    claimed: dict[str, str] = {}
    for start_id in starts:
        try:
            end_id, body = loop_block(schema, start_id)
        except ValueError:
            fail("LOOP_START has no matching LOOP_END", start_id)
            continue
        if end_id in claimed:
            fail(
                f"LOOP_END '{end_id}' is claimed by two loop starts "
                f"('{claimed[end_id]}' and '{start_id}')",
                end_id,
            )
            continue
        claimed[end_id] = start_id
        findings += _check_single_loop(schema, start_id, end_id, body)

    for end_id in ends - set(claimed):
        fail("LOOP_END has no matching LOOP_START", end_id)

    return findings


def _check_single_loop(
    schema: ProcessSchema, start_id: str, end_id: str, body: set[str]
) -> list[ValidationFinding]:
    """K6b-K6d for one paired loop block (see :func:`_check_k6_loops`)."""

    findings: list[ValidationFinding] = []

    def fail(message: str, node_id: str | None = None) -> None:
        findings.append(ValidationFinding(rule="K6", node_id=node_id, message=message))

    if not body:
        fail("loop body is empty (K6d)", start_id)
        return findings

    decision = schema.loop_decisions.get(end_id)
    if decision is None:
        fail("LOOP_END has no loop decision (K6b)", end_id)
        return findings
    element = schema.data_elements.get(decision.discriminator)
    if element is None:
        fail(
            f"loop discriminator '{decision.discriminator}' does not exist (K6b)",
            end_id,
        )
        return findings
    if element.source is not DataSourceKind.INSTANCE:
        fail(
            f"loop discriminator '{element.name}' must be an INSTANCE element (K6b)",
            end_id,
        )
    if not decision.cells:
        # Boolean shorthand (stage S1): repeat while value == repeat_value.
        if element.data_type is not DataType.BOOLEAN:
            fail(
                f"loop discriminator '{element.name}' must be BOOLEAN "
                f"(is {element.data_type.value}; K6b)",
                end_id,
            )
        if decision.kind is not XorDecisionKind.BOOLEAN:
            fail("a loop decision without cells must be of kind BOOLEAN (K6b)", end_id)
    else:
        # Partitioned decision (stage S3): the cells must tile the domain like
        # a K7 partition and classify it into at least one repeat and one exit
        # cell -- otherwise the loop could never terminate (all repeat) or
        # never repeat (all exit), both of which K6 rejects by construction.
        expected_kind = discriminator_kind(element.data_type)
        if expected_kind is None:
            fail(
                f"data type {element.data_type.value} cannot be used as a "
                "loop discriminator (K6b)",
                end_id,
            )
        elif expected_kind is not decision.kind:
            fail(
                f"loop decision kind {decision.kind.value} does not match "
                f"discriminator type {element.data_type.value} (K6b)",
                end_id,
            )
        _check_partition(end_id, decision.kind, decision.cells, fail, noun="loop")
        if all(cell.repeat for cell in decision.cells):
            fail("a loop partition needs at least one exit cell (K6b)", end_id)
        if all(not cell.repeat for cell in decision.cells):
            fail("a loop partition needs at least one repeat cell (K6b)", end_id)

    if decision.max_iterations is not None and decision.max_iterations < 2:
        # A bound of 1 (or less) would forbid every repetition -- the loop
        # would be pure ballast, which K6 rejects like an empty body (K6d).
        fail(
            f"max_iterations must allow at least one repetition (>= 2), "
            f"got {decision.max_iterations} (K6b)",
            end_id,
        )

    if decision.discriminator not in _written_through_body(schema, start_id, end_id, body):
        fail(
            f"loop discriminator '{element.name}' is not written on every path "
            f"through the loop body (K6c)",
            end_id,
        )
    return findings


def _written_through_body(
    schema: ProcessSchema, start_id: str, end_id: str, body: set[str]
) -> set[str]:
    """Elements guaranteed written on all paths from LOOP_START to LOOP_END.

    Block-local variant of :func:`_must_written_before`: the analysis is seeded
    empty at the loop start, so only writes *inside* the body count -- exactly
    the K6c requirement that every iteration re-decides on fresh data. Same
    join semantics as the global analysis (AND_JOIN unions, everything else
    intersects).
    """

    mandatory_writes: dict[str, set[str]] = {}
    for access in schema.data_accesses:
        if access.mode in WRITE_MODES and access.mandatory:
            mandatory_writes.setdefault(access.node_id, set()).add(access.element_id)

    block = body | {start_id, end_id}
    available_after: dict[str, set[str]] = {start_id: set()}
    for node_id in _topological_order(schema):
        if node_id not in block or node_id == start_id:
            continue
        preds = [
            e.source for e in schema.incoming(node_id) if e.source in block
        ]
        contributions = [available_after.get(p, set()) for p in preds]
        if not contributions:
            guaranteed: set[str] = set()
        elif schema.nodes[node_id].type is NodeType.AND_JOIN:
            guaranteed = set().union(*contributions)
        else:
            guaranteed = set(contributions[0]).intersection(*contributions[1:])
        if node_id == end_id:
            return guaranteed
        available_after[node_id] = guaranteed | mandatory_writes.get(node_id, set())
    return set()


# --- K4: cross-branch synchronisation edges --------------------------------


def _check_k4_sync_edges(schema: ProcessSchema) -> list[ValidationFinding]:
    """K4: sync edges only between activities of different AND branches.

    Fully additive -- a schema without SYNC edges produces no findings. A
    SYNC edge is an *ordering-only* wait (its target additionally waits until
    the source is completed or deselected); it never routes tokens, so the
    structural rules ignore it entirely (``incoming``/``outgoing`` are
    control-only). Checked here:

    - endpoints exist and are interactive ACTIVITY nodes,
    - source and target lie in **different branches of the same AND block**
      (the ADEPT constraint MR5 -- synchronising within one branch is plain
      control flow, across XOR branches it could dead-wait on a deselected
      path; the AND restriction plus the engine's completed-or-skipped
      resolution make a deadlock impossible),
    - the combined CONTROL+SYNC graph stays acyclic (two opposing sync edges
      would otherwise wait on each other forever).
    """

    findings: list[ValidationFinding] = []

    def fail(message: str, node_id: str | None = None) -> None:
        findings.append(ValidationFinding(rule="K4", node_id=node_id, message=message))

    syncs = [e for e in schema.edges if e.type is EdgeType.SYNC]
    if not syncs:
        return findings

    for edge in syncs:
        source = schema.nodes.get(edge.source)
        target = schema.nodes.get(edge.target)
        if source is None or target is None:
            fail("sync edge references an unknown node", edge.source)
            continue
        if source.type is not NodeType.ACTIVITY or target.type is not NodeType.ACTIVITY:
            fail("a sync edge connects only ACTIVITY nodes (K4)", edge.source)
            continue
        if not _in_different_and_branches(schema, edge.source, edge.target):
            fail(
                "sync edge must connect activities of different branches of "
                "the same AND block (K4)",
                edge.source,
            )

    if _cycle_with_sync(schema):
        fail("sync edges must not create a cycle with the control flow (K4)")
    return findings


def _and_branches(schema: ProcessSchema, split_id: str) -> list[set[str]]:
    """The node sets of each branch of an AND split (depth-counted walk).

    Same nesting-aware forward walk as :func:`procworks.model.loop_block`:
    a branch ends at the join that closes *this* split (depth 0). Only
    meaningful on K1-valid structures -- K4 runs after K1, and on a broken
    structure the resulting findings are noise on top of K1's anyway.
    """

    branches: list[set[str]] = []
    for edge in schema.outgoing(split_id):
        members: set[str] = set()
        stack: list[tuple[str, int]] = [(edge.target, 0)]
        while stack:
            node_id, depth = stack.pop()
            node = schema.nodes.get(node_id)
            if node is None or node_id in members:
                continue
            if node.type in JOIN_TYPES and depth == 0:
                continue  # the matching join closes the branch
            members.add(node_id)
            next_depth = depth
            if node.type in SPLIT_TYPES:
                next_depth += 1
            elif node.type in JOIN_TYPES:
                next_depth -= 1
            for out in schema.outgoing(node_id):
                stack.append((out.target, next_depth))
        branches.append(members)
    return branches


def _in_different_and_branches(schema: ProcessSchema, a: str, b: str) -> bool:
    """True when some AND split holds ``a`` and ``b`` in different branches."""

    for node in schema.nodes.values():
        if node.type is not NodeType.AND_SPLIT:
            continue
        branches = _and_branches(schema, node.id)
        index_a = next((i for i, m in enumerate(branches) if a in m), None)
        index_b = next((i for i, m in enumerate(branches) if b in m), None)
        if index_a is not None and index_b is not None and index_a != index_b:
            return True
    return False


def _cycle_with_sync(schema: ProcessSchema) -> bool:
    """True when CONTROL+SYNC together contain a cycle (Kahn over both)."""

    indegree = {nid: 0 for nid in schema.nodes}
    succ: dict[str, list[str]] = {nid: [] for nid in schema.nodes}
    for edge in schema.edges:
        if edge.source in indegree and edge.target in indegree:
            succ[edge.source].append(edge.target)
            indegree[edge.target] += 1
    queue: deque[str] = deque(nid for nid, deg in indegree.items() if deg == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        for nxt in succ[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    return visited != len(schema.nodes)


# --- K7: complete, overlap-free XOR branch partitions ---------------------


def _check_k7_xor_decisions(schema: ProcessSchema) -> list[ValidationFinding]:
    """Every XOR_SPLIT carries a total, disjoint, decidable branch partition.

    This is the constructive guarantee that an exclusive split can never
    deadlock (the partition is *total*: some branch always matches) nor activate
    several paths at once (it is *disjoint*: at most one branch matches). The
    partition is expressed over a typed discriminator element whose value is
    guaranteed to be set before the split is reached, so the property is decided
    at modelling time and preserved under every evolution step.
    """

    findings: list[ValidationFinding] = []

    def fail(message: str, node_id: str | None = None) -> None:
        findings.append(ValidationFinding(rule="K7", node_id=node_id, message=message))

    splits = {n.id for n in schema.nodes.values() if n.type is NodeType.XOR_SPLIT}

    # Stale decisions for nodes that are not (or no longer) XOR splits.
    for node_id in schema.xor_decisions:
        if node_id not in splits:
            fail("branch decision references a node that is not an XOR split", node_id)

    # A branch predicate may only ever sit on an edge leaving an XOR split.
    for edge in schema.edges:
        if edge.condition is not None and edge.source not in splits:
            fail("only edges leaving an XOR split may carry a branch condition", edge.source)

    written_before = _must_written_before(schema)

    for split_id in splits:
        decision = schema.xor_decisions.get(split_id)
        if decision is None:
            fail("XOR split has no branch decision", split_id)
            continue

        out_targets = sorted(e.target for e in schema.outgoing(split_id))
        branch_targets = sorted(b.target for b in decision.branches)
        if branch_targets != out_targets:
            fail("branch targets do not match the split's outgoing edges", split_id)
        if len(decision.branches) < 2:
            fail("an XOR split needs at least two branches", split_id)

        # An *empty* branch is a direct split -> join edge (its body was deleted
        # but its partition cell is kept). At most one is allowed: the runtime
        # keys edges by source+target, so two empty branches would collide on the
        # same split -> join edge. This also guarantees at least one non-empty
        # branch survives (a split with >= 2 branches can be at most all-but-one
        # empty). Enforced here as the No-Bypass backstop for every path (edit,
        # BPMN import, ad-hoc, migration).
        empty_branches = [
            b
            for b in decision.branches
            if (target := schema.nodes.get(b.target)) is not None
            and target.type is NodeType.XOR_JOIN
        ]
        if len(empty_branches) > 1:
            fail("an XOR split may carry at most one empty branch", split_id)

        element = schema.data_elements.get(decision.discriminator)
        if element is None:
            fail("discriminator data element does not exist", split_id)
            continue
        if element.source is not DataSourceKind.INSTANCE:
            fail("discriminator must be an instance data element", split_id)
        expected_kind = discriminator_kind(element.data_type)
        if expected_kind is None:
            fail(
                f"data type {element.data_type.value} cannot be used as an XOR discriminator",
                split_id,
            )
        elif expected_kind is not decision.kind:
            fail(
                f"decision kind {decision.kind.value} does not match "
                f"discriminator type {element.data_type.value}",
                split_id,
            )
        if decision.discriminator not in written_before.get(split_id, set()):
            fail(
                "discriminator may be unset when the split is reached "
                "(no guaranteed prior write)",
                split_id,
            )

        _check_partition(split_id, decision.kind, decision.branches, fail)

    return findings


def _check_partition(
    node_id: str,
    kind: XorDecisionKind,
    cells: Sequence[PartitionCell],
    fail: Callable[[str, str | None], None],
    *,
    noun: str = "split",
) -> None:
    """Check that ``cells`` tile the discriminator's domain (total + disjoint).

    Shared partition wellformedness for XOR branches (K7) and loop repeat/exit
    cells (K6b) -- both carry the same cell shape (:class:`PartitionCell`).
    ``noun`` only flavours the finding texts ("split" vs. "loop").
    """

    if kind is XorDecisionKind.THRESHOLD:
        last = len(cells) - 1
        prev: float | None = None
        for i, cell in enumerate(cells):
            if i == last:
                if cell.upper is not None:
                    fail("the last threshold branch must be unbounded (+inf)", node_id)
            elif cell.upper is None:
                fail("only the last threshold branch may be unbounded", node_id)
            else:
                if prev is not None and cell.upper <= prev:
                    fail("threshold bounds must be strictly ascending", node_id)
                prev = cell.upper
    elif kind is XorDecisionKind.BOOLEAN:
        if len(cells) != 2:
            fail(f"a boolean {noun} must have exactly two branches", node_id)
        truths = {c.bool_value for c in cells}
        if truths != {True, False}:
            fail("boolean branches must cover both true and false exactly once", node_id)
    else:  # ENUM
        else_count = sum(1 for c in cells if c.is_else)
        if else_count != 1:
            fail(f"an enum {noun} must have exactly one catch-all (otherwise) branch", node_id)
        seen: set[str] = set()
        for cell in cells:
            if cell.is_else:
                if cell.values:
                    fail("the catch-all branch must not list values", node_id)
                continue
            if not cell.values:
                fail("each enum branch must list at least one value", node_id)
            for value in cell.values:
                if value in seen:
                    fail(f"enum value {value!r} is matched by more than one branch", node_id)
                seen.add(value)


# --- K3: reachability (no isolated nodes, no dead ends) -------------------


def _check_k3_reachability(schema: ProcessSchema) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if not schema.nodes:
        return findings

    starts = [n for n in schema.nodes.values() if n.type is NodeType.START]
    ends = [n for n in schema.nodes.values() if n.type is NodeType.END]
    if len(starts) != 1 or len(ends) != 1:
        # Endpoint cardinality already reported by K2; skip to avoid noise.
        return findings

    forward = _bfs({s.id for s in starts}, _succ_map(schema))
    backward = _bfs({e.id for e in ends}, _pred_map(schema))

    for node in schema.nodes.values():
        if node.id not in forward:
            findings.append(
                ValidationFinding(
                    rule="K3", node_id=node.id, message="node not reachable from START"
                )
            )
        if node.id not in backward:
            findings.append(
                ValidationFinding(
                    rule="K3", node_id=node.id, message="node cannot reach END (dead end)"
                )
            )
    return findings


def _succ_map(schema: ProcessSchema) -> dict[str, list[str]]:
    # Control flow only: SYNC edges (K4) are ordering-only and must never
    # influence reachability, blocks or the must-analyses (conservative).
    out: dict[str, list[str]] = {nid: [] for nid in schema.nodes}
    for e in schema.edges:
        if e.type is EdgeType.CONTROL:
            out.setdefault(e.source, []).append(e.target)
    return out


def _pred_map(schema: ProcessSchema) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {nid: [] for nid in schema.nodes}
    for e in schema.edges:
        if e.type is EdgeType.CONTROL:
            out.setdefault(e.target, []).append(e.source)
    return out


def _bfs(starts: set[str], adjacency: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set(starts)
    queue: deque[str] = deque(starts)
    while queue:
        current = queue.popleft()
        for nxt in adjacency.get(current, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


# --- D1-D4: data-flow correctness ----------------------------------------


def _check_data_flow(schema: ProcessSchema) -> list[ValidationFinding]:
    """Run data-flow rules D4 (well-formedness), D3 (types), D2 and D1."""

    findings: list[ValidationFinding] = []
    findings += _check_d4_wellformed(schema)
    findings += _check_d3_types(schema)
    # D1/D2 rely on a well-formed control graph; skip if the structure or the
    # data accesses are already broken to avoid noisy follow-up errors.
    if findings or _structure_broken(schema):
        return findings
    findings += _check_d2_concurrent_writes(schema)
    findings += _check_d1_supply(schema)
    return findings


def _structure_broken(schema: ProcessSchema) -> bool:
    """True if structural rules already fail (then D1/D2 are not meaningful)."""

    return bool(
        _check_k2_endpoints_and_degrees(schema)
        or _check_k1_gateways(schema)
        or _check_k3_reachability(schema)
    )


def _check_d4_wellformed(schema: ProcessSchema) -> list[ValidationFinding]:
    """D4: data accesses only on ACTIVITY nodes and to existing elements."""

    findings: list[ValidationFinding] = []
    for access in schema.data_accesses:
        node = schema.nodes.get(access.node_id)
        if node is None:
            findings.append(
                ValidationFinding(
                    rule="D4",
                    node_id=access.node_id,
                    message=f"data access references unknown node '{access.node_id}'",
                )
            )
        elif node.type is not NodeType.ACTIVITY:
            findings.append(
                ValidationFinding(
                    rule="D4",
                    node_id=access.node_id,
                    message=f"only ACTIVITY nodes may access data, not {node.type.value}",
                )
            )
        if access.element_id not in schema.data_elements:
            findings.append(
                ValidationFinding(
                    rule="D4",
                    node_id=access.node_id,
                    message=f"data access references unknown element '{access.element_id}'",
                )
            )
    return findings


def _check_d3_types(schema: ProcessSchema) -> list[ValidationFinding]:
    """D3: declared parameter type must match the data element type."""

    findings: list[ValidationFinding] = []
    for access in schema.data_accesses:
        element = schema.data_elements.get(access.element_id)
        if element is None or access.param_type is None:
            continue
        if access.param_type != element.data_type:
            findings.append(
                ValidationFinding(
                    rule="D3",
                    node_id=access.node_id,
                    message=(
                        f"parameter type {access.param_type.value} does not match "
                        f"element '{element.name}' type {element.data_type.value}"
                    ),
                )
            )
    return findings


def _connector_supplied(element: DataElement) -> bool:
    """Is this element's value produced by a connector rather than by the process?

    An EXTERNAL element bound for **reading** -- record-bound (``external``,
    C1-C3) or scalar-select-bound (``select``, C4-C6) -- is resolved by the DAL
    when the reading node runs (:meth:`procworks.dal.DataAccessLayer.read`).
    There is no process step that writes it, so demanding a prior mandatory
    write (D1) would make such an element **impossible** to read as a mandatory
    input. Its supply guarantee is carried instead by the connector rules: the
    lookup key / filter sources must be must-written before every reader
    (C2 and C5, concept §9.2 -- "das Schlüssel-Datenelement muss vorher gesetzt
    sein").

    A ``write``-bound element (C7-C9) is the opposite case: the *process*
    produces the value and it is flushed outward, so a mandatory read of it does
    require a prior write and is deliberately **not** exempted here.
    """

    return element.source is DataSourceKind.EXTERNAL and (
        element.external is not None or element.select is not None
    )


def _check_d1_supply(schema: ProcessSchema) -> list[ValidationFinding]:
    """D1: every mandatory read is supplied by a mandatory write on all paths."""

    findings: list[ValidationFinding] = []
    written_before = _must_written_before(schema)
    for access in schema.data_accesses:
        if access.mode not in READ_MODES or not access.mandatory:
            continue
        element = schema.data_elements.get(access.element_id)
        if element is None:
            continue
        if _connector_supplied(element):
            continue  # supplied by the connector; key coverage is C2/C5
        if access.element_id not in written_before.get(access.node_id, set()):
            findings.append(
                ValidationFinding(
                    rule="D1",
                    node_id=access.node_id,
                    message=(
                        f"mandatory input '{element.name}' may be read before it is "
                        f"written on some execution path"
                    ),
                )
            )
    return findings


def _subprocess_output_writes(schema: ProcessSchema) -> dict[str, set[str]]:
    """Parent elements each SUBPROCESS node writes back via its output mapping.

    At runtime :func:`execution._join_subprocess` copies the child's mapped
    outputs into these parent elements, so they behave like a mandatory write of
    the sub-process node in the parent's data-flow analysis. Only mappings whose
    parent element actually exists are counted (H2 reports unknown ones).
    """

    writes: dict[str, set[str]] = {}
    for node_id, binding in schema.sub_process_bindings.items():
        node = schema.nodes.get(node_id)
        if node is None or node.type is not NodeType.SUBPROCESS:
            continue
        for parent_eid in binding.output_mapping.values():
            if parent_eid in schema.data_elements:
                writes.setdefault(node_id, set()).add(parent_eid)
    return writes


def _must_written_before(schema: ProcessSchema) -> dict[str, set[str]]:
    """For each node, the elements guaranteed written on all paths before it.

    Forward must-analysis over the (acyclic) control graph: at an AND_JOIN all
    branches run, so contributions are unioned; at an XOR_JOIN only one branch
    runs, so contributions are intersected.
    """

    order = _topological_order(schema)
    pred = _pred_map(schema)
    mandatory_writes: dict[str, set[str]] = {nid: set() for nid in schema.nodes}
    for access in schema.data_accesses:
        if access.mode in WRITE_MODES and access.mandatory:
            mandatory_writes.setdefault(access.node_id, set()).add(access.element_id)
    # A SUBPROCESS writes its mapped outputs back into the parent when it joins
    # (Datenübergabe), so those parent elements are guaranteed available once the
    # sub-process node completes -- exactly like a mandatory write.
    for node_id, produced in _subprocess_output_writes(schema).items():
        mandatory_writes.setdefault(node_id, set()).update(produced)

    before: dict[str, set[str]] = {nid: set() for nid in schema.nodes}
    available_after: dict[str, set[str]] = {}
    for node_id in order:
        predecessors = pred.get(node_id, [])
        if not predecessors:
            guaranteed: set[str] = set()
        else:
            contributions = [available_after.get(p, set()) for p in predecessors]
            if schema.nodes[node_id].type is NodeType.AND_JOIN:
                guaranteed = set().union(*contributions)
            else:
                guaranteed = set(contributions[0]).intersection(*contributions[1:])
        before[node_id] = guaranteed
        available_after[node_id] = guaranteed | mandatory_writes.get(node_id, set())
    return before


def _check_d2_concurrent_writes(schema: ProcessSchema) -> list[ValidationFinding]:
    """D2: no two mandatory writes to the same element on parallel AND branches."""

    findings: list[ValidationFinding] = []
    succ = _succ_map(schema)
    reachable = {nid: _bfs(set(succ.get(nid, [])), succ) for nid in schema.nodes}

    writers: dict[str, list[str]] = {}
    for access in schema.data_accesses:
        if access.mode in WRITE_MODES and access.mandatory:
            writers.setdefault(access.element_id, []).append(access.node_id)
    # Sub-process output write-backs count as mandatory writes too (D2 must see
    # them so two parallel sub-processes cannot race on the same parent element).
    for node_id, produced in _subprocess_output_writes(schema).items():
        for element_id in produced:
            writers.setdefault(element_id, []).append(node_id)

    for element_id, nodes in writers.items():
        unique = sorted(set(nodes))
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                a, b = unique[i], unique[j]
                if b in reachable[a] or a in reachable[b]:
                    continue  # sequentially ordered -> not concurrent
                if _parallel_under_and(schema, a, b, reachable):
                    element = schema.data_elements.get(element_id)
                    name = element.name if element else element_id
                    findings.append(
                        ValidationFinding(
                            rule="D2",
                            node_id=a,
                            message=(
                                f"concurrent writes to data element '{name}' on "
                                f"parallel AND branches ({a}, {b})"
                            ),
                        )
                    )
    return findings


def _parallel_under_and(
    schema: ProcessSchema, a: str, b: str, reachable: dict[str, set[str]]
) -> bool:
    """True if a and b sit on different branches of a common AND_SPLIT."""

    common_splits = [
        nid
        for nid, node in schema.nodes.items()
        if node.type in SPLIT_TYPES and a in reachable[nid] and b in reachable[nid]
    ]
    if not common_splits:
        return False
    # Innermost common split: the one reachable from all other common splits.
    for candidate in common_splits:
        if all(other == candidate or candidate in reachable[other] for other in common_splits):
            return schema.nodes[candidate].type is NodeType.AND_SPLIT
    return False


def _topological_order(schema: ProcessSchema) -> list[str]:
    succ = _succ_map(schema)
    indegree = {nid: 0 for nid in schema.nodes}
    for edge in schema.edges:
        if edge.type is not EdgeType.CONTROL:
            continue  # SYNC is ordering-only (K4), not part of the structure
        indegree[edge.target] = indegree.get(edge.target, 0) + 1
    queue: deque[str] = deque(nid for nid, deg in indegree.items() if deg == 0)
    order: list[str] = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for nxt in succ.get(node_id, []):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    return order


def _must_executed_before(schema: ProcessSchema) -> dict[str, set[str]]:
    """For each node, the nodes guaranteed to have executed on all prior paths.

    Same must-analysis as the data flow (AND_JOIN unions branches, XOR_JOIN
    intersects them), but tracking node execution instead of data writes. Used
    by Z3 to validate NodePerformingAgent back-references.
    """

    order = _topological_order(schema)
    pred = _pred_map(schema)
    before: dict[str, set[str]] = {nid: set() for nid in schema.nodes}
    executed_after: dict[str, set[str]] = {}
    for node_id in order:
        predecessors = pred.get(node_id, [])
        if not predecessors:
            guaranteed: set[str] = set()
        else:
            contributions = [executed_after.get(p, set()) for p in predecessors]
            if schema.nodes[node_id].type is NodeType.AND_JOIN:
                guaranteed = set().union(*contributions)
            else:
                guaranteed = set(contributions[0]).intersection(*contributions[1:])
        before[node_id] = guaranteed
        executed_after[node_id] = guaranteed | {node_id}
    return before


# --- U1-U3: input-mask (form designer) well-formedness -------------------


def _check_forms(schema: ProcessSchema) -> list[ValidationFinding]:
    """Input-mask rules U1-U3 (additive; silent for models without masks).

    A form is a presentation layer over ``data_accesses``: every field mirrors a
    read/write link. These rules keep mask and data flow consistent so that the
    Correct-by-Construction guarantee -- in particular D1 (no read without a
    write on every path) -- also holds for masks. U-rules never fire unless a
    schema carries at least one mask.
    """

    findings: list[ValidationFinding] = []
    if not schema.forms:
        return findings

    for node_id, form in schema.forms.items():
        node = schema.nodes.get(node_id)
        if node is None:
            findings.append(
                ValidationFinding(
                    rule="U1",
                    node_id=node_id,
                    message=f"input mask references unknown node '{node_id}'",
                )
            )
            continue
        if node.type is not NodeType.ACTIVITY:
            findings.append(
                ValidationFinding(
                    rule="U1",
                    node_id=node_id,
                    message="input masks are only allowed on ACTIVITY nodes",
                )
            )

        accesses = schema.accesses_of(node_id)
        seen_field_ids: set[str] = set()
        seen_elements: set[str] = set()
        for field in form.fields:
            if field.id in seen_field_ids:
                findings.append(
                    ValidationFinding(
                        rule="U2",
                        node_id=node_id,
                        message=f"duplicate field id '{field.id}' in input mask",
                    )
                )
            seen_field_ids.add(field.id)
            if field.element_id in seen_elements:
                findings.append(
                    ValidationFinding(
                        rule="U2",
                        node_id=node_id,
                        message=(
                            f"element '{field.element_id}' is bound by more than one "
                            "field in the same mask"
                        ),
                    )
                )
            seen_elements.add(field.element_id)
            if not field.label.strip():
                findings.append(
                    ValidationFinding(
                        rule="U2",
                        node_id=node_id,
                        message=f"field '{field.id}' has an empty label",
                    )
                )

            element = schema.data_elements.get(field.element_id)
            if element is None:
                findings.append(
                    ValidationFinding(
                        rule="U1",
                        node_id=node_id,
                        message=(
                            f"field '{field.id}' references unknown data element "
                            f"'{field.element_id}'"
                        ),
                    )
                )
                continue

            if not widget_matches_type(field.widget, element.data_type):
                findings.append(
                    ValidationFinding(
                        rule="U2",
                        node_id=node_id,
                        message=(
                            f"widget '{field.widget}' cannot present element "
                            f"'{field.element_id}' of type '{element.data_type}'"
                        ),
                    )
                )

            if field.widget is WidgetKind.DROPDOWN:
                options = [o.strip() for o in field.options]
                if len(field.options) < 2 or any(not o for o in options):
                    findings.append(
                        ValidationFinding(
                            rule="U2",
                            node_id=node_id,
                            message=(
                                f"dropdown field '{field.id}' needs at least two "
                                "non-empty options"
                            ),
                        )
                    )
                elif len(set(options)) != len(options):
                    findings.append(
                        ValidationFinding(
                            rule="U2",
                            node_id=node_id,
                            message=f"dropdown field '{field.id}' has duplicate options",
                        )
                    )
            elif field.options:
                findings.append(
                    ValidationFinding(
                        rule="U2",
                        node_id=node_id,
                        message=(
                            f"field '{field.id}' carries options but its widget is "
                            f"not a dropdown"
                        ),
                    )
                )

            # U3: the field must be backed by a matching data access. This is the
            # bridge that lets D1 govern "no read without a prior write".
            modes = {a.mode for a in accesses if a.element_id == field.element_id}
            if field.mode in WRITE_MODES and not any(m in WRITE_MODES for m in modes):
                findings.append(
                    ValidationFinding(
                        rule="U3",
                        node_id=node_id,
                        message=(
                            f"input field '{field.id}' has no write access for element "
                            f"'{field.element_id}'"
                        ),
                    )
                )
            if field.mode in READ_MODES and not any(m in READ_MODES for m in modes):
                findings.append(
                    ValidationFinding(
                        rule="U3",
                        node_id=node_id,
                        message=(
                            f"display field '{field.id}' has no read access for element "
                            f"'{field.element_id}'"
                        ),
                    )
                )
    return findings


# --- C1-C3: external data connectors -------------------------------------


def _check_connectors(schema: ProcessSchema) -> list[ValidationFinding]:
    """Connector rules C1-C3 for EXTERNAL data elements (Section 9).

    C1: an EXTERNAL element carries an ``external`` binding to a registered
        connector (and an INSTANCE element carries none).
    C2: the binding's key references an existing INSTANCE data element (the
        process supplies the lookup key; it is not itself external) and is not
        the element itself.
    C3: the bound entity name is non-empty.
    """

    findings: list[ValidationFinding] = []
    for element in schema.data_elements.values():
        bindings = [
            b for b in (element.external, element.select, element.write) if b is not None
        ]
        if element.source is DataSourceKind.INSTANCE:
            if bindings:
                findings.append(
                    ValidationFinding(
                        rule="C1",
                        message=(
                            f"INSTANCE element '{element.id}' must not carry an "
                            f"external binding"
                        ),
                    )
                )
            continue
        if len(bindings) > 1:
            findings.append(
                ValidationFinding(
                    rule="C1",
                    message=(
                        f"element '{element.id}' must carry exactly one external "
                        f"binding kind (record, scalar-select or scalar-write)"
                    ),
                )
            )
            continue
        if element.select is not None or element.write is not None:
            # Scalar-bound EXTERNAL element: the record rules C1-C3 do not apply;
            # well-formedness/typing is checked by C4-C6 (select) / C7-C9 (write).
            continue
        binding = element.external
        if binding is None:
            findings.append(
                ValidationFinding(
                    rule="C1",
                    message=f"EXTERNAL element '{element.id}' is missing its external binding",
                )
            )
            continue
        if binding.connector_id not in schema.connectors:
            findings.append(
                ValidationFinding(
                    rule="C1",
                    message=(
                        f"element '{element.id}' references unknown connector "
                        f"'{binding.connector_id}'"
                    ),
                )
            )
        if not binding.entity.strip():
            findings.append(
                ValidationFinding(
                    rule="C3",
                    message=f"element '{element.id}' has an empty connector entity",
                )
            )
        if binding.key_element_id == element.id:
            findings.append(
                ValidationFinding(
                    rule="C2",
                    message=f"element '{element.id}' uses itself as its lookup key",
                )
            )
            continue
        key_element = schema.data_elements.get(binding.key_element_id)
        if key_element is None:
            findings.append(
                ValidationFinding(
                    rule="C2",
                    message=(
                        f"element '{element.id}' uses unknown key element "
                        f"'{binding.key_element_id}'"
                    ),
                )
            )
        elif key_element.source is not DataSourceKind.INSTANCE:
            findings.append(
                ValidationFinding(
                    rule="C2",
                    message=(
                        f"key element '{binding.key_element_id}' of '{element.id}' must be "
                        f"an INSTANCE element"
                    ),
                )
            )
        else:
            findings += _check_key_supplied_before_readers(
                schema, element.id, binding.key_element_id
            )
    return findings


def _check_key_supplied_before_readers(
    schema: ProcessSchema, element_id: str, key_element_id: str
) -> list[ValidationFinding]:
    """C2 (D1 coupling): the lookup key is set before anything reads the element.

    The record-bound counterpart of the coupling C5 already performs for
    scalar-select filters. Because a connector-supplied element is exempt from
    the D1 write requirement (:func:`_connector_supplied`), *this* is what keeps
    the supply guarantee intact: when the DAL resolves the element it reads the
    key from the instance values, so the key must be must-written on every path
    to every reading node -- otherwise the lookup would run with a missing key
    and fail at runtime instead of at modelling time (concept §9.2).

    Skipped on a structurally broken schema, where the must-analysis is not
    meaningful (same guard as D1/D2).
    """

    findings: list[ValidationFinding] = []
    readers = [
        a.node_id
        for a in schema.data_accesses
        if a.element_id == element_id and a.mode in READ_MODES
    ]
    if not readers or _structure_broken(schema):
        return findings
    written_before = _must_written_before(schema)
    for node_id in sorted(set(readers)):
        if key_element_id not in written_before.get(node_id, set()):
            key = schema.data_elements.get(key_element_id)
            findings.append(
                ValidationFinding(
                    rule="C2",
                    node_id=node_id,
                    message=(
                        f"lookup key '{key.name if key else key_element_id}' of external "
                        f"element '{element_id}' is not guaranteed to be set on every "
                        f"path to this reader"
                    ),
                )
            )
    return findings


# --- C4-C6: structured scalar SQL-select bindings ------------------------

#: Filter operators that require an orderable type (numeric or date).
_ORDER_OPERATORS = frozenset(
    {FilterOperator.LT, FilterOperator.LE, FilterOperator.GT, FilterOperator.GE}
)
#: Data types that support ordering comparisons.
_ORDERABLE_TYPES = frozenset({DataType.INTEGER, DataType.FLOAT, DataType.DATE})


def _operator_matches_type(operator: FilterOperator, data_type: DataType) -> bool:
    """Whether a filter ``operator`` is valid for a column of ``data_type`` (C5)."""

    if operator is FilterOperator.LIKE:
        return data_type is DataType.STRING
    if operator in _ORDER_OPERATORS:
        return data_type in _ORDERABLE_TYPES
    return True  # EQ / NE / IN apply to any type


def _check_query_cardinality(
    element_id: str, binding: SqlSelectBinding
) -> list[ValidationFinding]:
    """C6: the select must structurally guarantee at most one result row."""

    findings: list[ValidationFinding] = []
    if binding.cardinality is Cardinality.KEY_UNIQUE:
        if not binding.unique_column.strip():
            findings.append(
                ValidationFinding(
                    rule="C6",
                    message=(
                        f"element '{element_id}' uses KEY_UNIQUE but declares no "
                        f"unique column"
                    ),
                )
            )
        elif not any(
            f.operator is FilterOperator.EQ and f.column == binding.unique_column
            for f in binding.filters
        ):
            findings.append(
                ValidationFinding(
                    rule="C6",
                    message=(
                        f"element '{element_id}' uses KEY_UNIQUE but has no equality "
                        f"filter on unique column '{binding.unique_column}'"
                    ),
                )
            )
    elif binding.cardinality is Cardinality.AGGREGATE:
        if binding.aggregate is AggregateKind.NONE:
            findings.append(
                ValidationFinding(
                    rule="C6",
                    message=(
                        f"element '{element_id}' uses AGGREGATE cardinality but "
                        f"projects a plain column"
                    ),
                )
            )
    elif not binding.order_by:  # FIRST_ORDERED
        findings.append(
            ValidationFinding(
                rule="C6",
                message=(
                    f"element '{element_id}' uses FIRST_ORDERED but has an empty "
                    f"ORDER BY"
                ),
            )
        )
    return findings


def _check_scalar_queries(schema: ProcessSchema) -> list[ValidationFinding]:
    """Structured scalar SQL-select rules C4-C6 (concept §6).

    Silent unless a data element carries a ``select`` binding, so it never
    affects models without scalar SQL bindings (fully additive).

    C4: the select projection's result type (derived via
        :func:`aggregate_result_type`) matches the element's declared type -- the
        result *fits* the data element it fills.
    C5: connector/entity/column are well-formed; every filter references an
        existing INSTANCE source element of matching type with a type-compatible
        operator; and each filter source is guaranteed written on every path
        before any node that reads the element (D1 coupling, like the K7
        discriminator).
    C6: the select structurally yields at most one row (see
        :func:`_check_query_cardinality`).
    """

    if not any(el.select is not None for el in schema.data_elements.values()):
        return []

    findings: list[ValidationFinding] = []
    written_before = _must_written_before(schema)
    for element in schema.data_elements.values():
        binding = element.select
        if binding is None or element.source is not DataSourceKind.EXTERNAL:
            # INSTANCE elements carrying a select are already reported by C1.
            continue

        # C4: the projection result type must match the element type.
        result_type = aggregate_result_type(binding.aggregate, binding.column_type)
        if result_type is not element.data_type:
            findings.append(
                ValidationFinding(
                    rule="C4",
                    message=(
                        f"element '{element.id}' is {element.data_type.value} but its "
                        f"select projection yields {result_type.value}"
                    ),
                )
            )

        # C5: connector / entity / column well-formedness.
        if binding.connector_id not in schema.connectors:
            findings.append(
                ValidationFinding(
                    rule="C5",
                    message=(
                        f"element '{element.id}' references unknown connector "
                        f"'{binding.connector_id}'"
                    ),
                )
            )
        if not binding.entity.strip():
            findings.append(
                ValidationFinding(
                    rule="C5",
                    message=f"element '{element.id}' has an empty select entity",
                )
            )
        if not binding.column.strip():
            findings.append(
                ValidationFinding(
                    rule="C5",
                    message=f"element '{element.id}' has an empty projection column",
                )
            )

        # C5: filters -- source existence/type, operator compatibility, D1 coupling.
        reading_nodes = [
            access.node_id
            for access in schema.data_accesses
            if access.element_id == element.id and access.mode in READ_MODES
        ]
        for item in binding.filters:
            if not _operator_matches_type(item.operator, item.column_type):
                findings.append(
                    ValidationFinding(
                        rule="C5",
                        message=(
                            f"element '{element.id}' uses operator {item.operator.value} "
                            f"on a {item.column_type.value} filter column"
                        ),
                    )
                )
            source = schema.data_elements.get(item.key_element_id)
            if source is None:
                findings.append(
                    ValidationFinding(
                        rule="C5",
                        message=(
                            f"element '{element.id}' uses unknown filter source "
                            f"'{item.key_element_id}'"
                        ),
                    )
                )
                continue
            if source.source is not DataSourceKind.INSTANCE:
                findings.append(
                    ValidationFinding(
                        rule="C5",
                        message=(
                            f"filter source '{item.key_element_id}' of '{element.id}' "
                            f"must be an INSTANCE element"
                        ),
                    )
                )
                continue
            if source.data_type is not item.column_type:
                findings.append(
                    ValidationFinding(
                        rule="C5",
                        message=(
                            f"filter column type {item.column_type.value} of "
                            f"'{element.id}' does not match source "
                            f"'{item.key_element_id}' ({source.data_type.value})"
                        ),
                    )
                )
            for node_id in reading_nodes:
                if item.key_element_id not in written_before.get(node_id, set()):
                    findings.append(
                        ValidationFinding(
                            rule="C5",
                            node_id=node_id,
                            message=(
                                f"filter source '{item.key_element_id}' of "
                                f"'{element.id}' may be read before it is written on "
                                f"some execution path"
                            ),
                        )
                    )

        # C6: cardinality guarantee.
        findings += _check_query_cardinality(element.id, binding)
    return findings


# --- C7-C9: structured scalar SQL write-back bindings --------------------


def _check_scalar_writes(schema: ProcessSchema) -> list[ValidationFinding]:
    """Structured scalar SQL write-back rules C7-C9 (concept §7, Q4).

    Silent unless a data element carries a ``write`` binding (fully additive).

    C7: the target column's declared type matches the element's type -- the
        written scalar *fits* the column it updates.
    C8: connector/entity/column are well-formed; every filter references an
        existing INSTANCE source element of matching type with a type-compatible
        operator; and each filter source is guaranteed written on every path
        before any node that writes the element (D1 coupling).
    C9: the write targets exactly one row -- a declared ``unique_column`` with an
        equality filter on it (an UPDATE never fans out to many rows).
    """

    if not any(el.write is not None for el in schema.data_elements.values()):
        return []

    findings: list[ValidationFinding] = []
    written_before = _must_written_before(schema)
    for element in schema.data_elements.values():
        binding = element.write
        if binding is None or element.source is not DataSourceKind.EXTERNAL:
            continue

        # C7: the target column type must match the element type.
        if binding.column_type is not element.data_type:
            findings.append(
                ValidationFinding(
                    rule="C7",
                    message=(
                        f"element '{element.id}' is {element.data_type.value} but its "
                        f"write target column is {binding.column_type.value}"
                    ),
                )
            )

        # C8: connector / entity / column well-formedness.
        if binding.connector_id not in schema.connectors:
            findings.append(
                ValidationFinding(
                    rule="C8",
                    message=(
                        f"element '{element.id}' references unknown connector "
                        f"'{binding.connector_id}'"
                    ),
                )
            )
        if not binding.entity.strip():
            findings.append(
                ValidationFinding(
                    rule="C8",
                    message=f"element '{element.id}' has an empty write entity",
                )
            )
        if not binding.column.strip():
            findings.append(
                ValidationFinding(
                    rule="C8",
                    message=f"element '{element.id}' has an empty target column",
                )
            )

        # C8: filters -- source existence/type, operator compatibility, D1 coupling.
        writing_nodes = [
            access.node_id
            for access in schema.data_accesses
            if access.element_id == element.id and access.mode in WRITE_MODES
        ]
        for item in binding.filters:
            if not _operator_matches_type(item.operator, item.column_type):
                findings.append(
                    ValidationFinding(
                        rule="C8",
                        message=(
                            f"element '{element.id}' uses operator {item.operator.value} "
                            f"on a {item.column_type.value} filter column"
                        ),
                    )
                )
            source = schema.data_elements.get(item.key_element_id)
            if source is None:
                findings.append(
                    ValidationFinding(
                        rule="C8",
                        message=(
                            f"element '{element.id}' uses unknown filter source "
                            f"'{item.key_element_id}'"
                        ),
                    )
                )
                continue
            if source.source is not DataSourceKind.INSTANCE:
                findings.append(
                    ValidationFinding(
                        rule="C8",
                        message=(
                            f"filter source '{item.key_element_id}' of '{element.id}' "
                            f"must be an INSTANCE element"
                        ),
                    )
                )
                continue
            if source.data_type is not item.column_type:
                findings.append(
                    ValidationFinding(
                        rule="C8",
                        message=(
                            f"filter column type {item.column_type.value} of "
                            f"'{element.id}' does not match source "
                            f"'{item.key_element_id}' ({source.data_type.value})"
                        ),
                    )
                )
            for node_id in writing_nodes:
                if item.key_element_id not in written_before.get(node_id, set()):
                    findings.append(
                        ValidationFinding(
                            rule="C8",
                            node_id=node_id,
                            message=(
                                f"filter source '{item.key_element_id}' of "
                                f"'{element.id}' may be used before it is written on "
                                f"some execution path"
                            ),
                        )
                    )

        # C9: single-row write guarantee.
        if not binding.unique_column.strip():
            findings.append(
                ValidationFinding(
                    rule="C9",
                    message=(
                        f"write of '{element.id}' declares no unique column -- an "
                        f"UPDATE must target exactly one row"
                    ),
                )
            )
        elif not any(
            f.operator is FilterOperator.EQ and f.column == binding.unique_column
            for f in binding.filters
        ):
            findings.append(
                ValidationFinding(
                    rule="C9",
                    message=(
                        f"write of '{element.id}' has no equality filter on unique "
                        f"column '{binding.unique_column}'"
                    ),
                )
            )
    return findings


# --- Z1-Z4: resource / staff-assignment correctness ----------------------


def _check_resources(schema: ProcessSchema) -> list[ValidationFinding]:
    """Run resource rules Z1 (well-formed), Z4 (service), the activity
    repository rules A1-A3, and (if well-formed) Z2 and Z3."""

    findings: list[ValidationFinding] = []
    findings += _check_z1_wellformed(schema)
    findings += _check_org_master_data(schema)
    findings += _check_z4_service(schema)
    findings += _check_activity_repository(schema)
    # Z2/Z3 evaluate the rule and the control graph; only run them when the
    # rules are well-formed (Z1) and the structure is intact.
    if findings or _structure_broken(schema):
        return findings
    findings += _check_z2_resolvable(schema)
    findings += _check_z3_backrefs(schema)
    return findings


def _check_org_master_data(schema: ProcessSchema) -> list[ValidationFinding]:
    """Z1: referential integrity of org master data (managers and deputies).

    A unit's ``manager_id`` and an agent's ``deputy_id`` must reference an
    existing agent; an agent cannot be its own deputy. Deputy chains may form
    cycles in principle -- runtime resolution follows them with a visited
    guard, so cycles are tolerated rather than rejected here.
    """

    findings: list[ValidationFinding] = []
    org = schema.org_model
    for unit in org.org_units.values():
        if unit.manager_id is not None and unit.manager_id not in org.agents:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    message=f"org unit '{unit.id}' has unknown manager '{unit.manager_id}'",
                )
            )
    for agent in org.agents.values():
        if agent.deputy_id is None:
            continue
        if agent.deputy_id == agent.id:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    message=f"agent '{agent.id}' cannot be its own deputy",
                )
            )
        elif agent.deputy_id not in org.agents:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    message=f"agent '{agent.id}' has unknown deputy '{agent.deputy_id}'",
                )
            )
    return findings


def _check_z1_wellformed(schema: ProcessSchema) -> list[ValidationFinding]:
    """Z1: staff rules are well-formed and reference existing elements."""

    findings: list[ValidationFinding] = []
    for node_id, rule in schema.staff_rules.items():
        node = schema.nodes.get(node_id)
        if node is None:
            findings.append(
                ValidationFinding(
                    rule="Z1", node_id=node_id, message=f"staff rule on unknown node '{node_id}'"
                )
            )
        elif node.type is not NodeType.ACTIVITY:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    node_id=node_id,
                    message=(
                        f"staff rules are only allowed on ACTIVITY nodes, "
                        f"not {node.type.value}"
                    ),
                )
            )
        findings += _check_staff_rule_node(schema, node_id, rule)
    return findings


def _check_staff_rule_node(
    schema: ProcessSchema, node_id: str, rule: StaffRule
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if rule.kind in STAFF_LEAF_KINDS:
        if rule.operands:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    node_id=node_id,
                    message=f"{rule.kind.value} term must have no operands",
                )
            )
        if rule.ref is None:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    node_id=node_id,
                    message=f"{rule.kind.value} term requires a reference",
                )
            )
        else:
            findings += _check_staff_ref(schema, node_id, rule)
    elif rule.kind in STAFF_COMBINATOR_KINDS:
        min_operands = 2 if rule.kind is StaffRuleKind.EXCEPT else 1
        if rule.kind is StaffRuleKind.EXCEPT and len(rule.operands) != 2:
            findings.append(
                ValidationFinding(
                    rule="Z1", node_id=node_id, message="EXCEPT requires exactly two operands"
                )
            )
        elif len(rule.operands) < min_operands:
            findings.append(
                ValidationFinding(
                    rule="Z1",
                    node_id=node_id,
                    message=f"{rule.kind.value} requires at least {min_operands} operand(s)",
                )
            )
        for operand in rule.operands:
            findings += _check_staff_rule_node(schema, node_id, operand)
    return findings


def _check_staff_ref(
    schema: ProcessSchema, node_id: str, rule: StaffRule
) -> list[ValidationFinding]:
    org = schema.org_model
    ref = rule.ref
    if rule.kind is StaffRuleKind.ROLE and ref not in org.roles:
        return [
            ValidationFinding(rule="Z1", node_id=node_id, message=f"unknown role '{ref}'")
        ]
    if rule.kind is StaffRuleKind.ORG_UNIT and ref not in org.org_units:
        return [
            ValidationFinding(rule="Z1", node_id=node_id, message=f"unknown org unit '{ref}'")
        ]
    if rule.kind is StaffRuleKind.AGENT and ref not in org.agents:
        return [
            ValidationFinding(rule="Z1", node_id=node_id, message=f"unknown agent '{ref}'")
        ]
    if rule.kind in STAFF_NODE_REF_KINDS and ref not in schema.nodes:
        return [
            ValidationFinding(
                rule="Z1",
                node_id=node_id,
                message=f"{rule.kind.value} references unknown node '{ref}'",
            )
        ]
    return []


def _check_z4_service(schema: ProcessSchema) -> list[ValidationFinding]:
    """Z4: service bindings are well-formed; automatic steps carry no staff rule."""

    findings: list[ValidationFinding] = []
    for node_id, binding in schema.service_bindings.items():
        node = schema.nodes.get(node_id)
        if node is None or node.type is not NodeType.ACTIVITY:
            findings.append(
                ValidationFinding(
                    rule="Z4",
                    node_id=node_id,
                    message="service binding is only allowed on ACTIVITY nodes",
                )
            )
            continue
        if binding.automatic and node_id in schema.staff_rules:
            findings.append(
                ValidationFinding(
                    rule="Z4",
                    node_id=node_id,
                    message="automatic step must not carry a staff rule (BZR)",
                )
            )
    return findings


def _check_activity_repository(schema: ProcessSchema) -> list[ValidationFinding]:
    """Activity Repository rules A1-A3 for template-bound services.

    A1: a referenced template must exist in the repository.
    A2: the binding's ``automatic`` flag must match the template's executor.
    A3: the template interface must be bound type-conformantly -- every
        mandatory parameter is mapped, mapped names belong to the template, and
        each mapped data element exists with a matching type.
    Free-form bindings (no ``template_id``) are left untouched.
    """

    findings: list[ValidationFinding] = []
    for node_id, binding in schema.service_bindings.items():
        if binding.template_id is None:
            continue
        template = schema.activity_templates.get(binding.template_id)
        if template is None:
            findings.append(
                ValidationFinding(
                    rule="A1",
                    node_id=node_id,
                    message=f"service binding references unknown template '{binding.template_id}'",
                )
            )
            continue
        if binding.automatic != template.is_automatic:
            findings.append(
                ValidationFinding(
                    rule="A2",
                    node_id=node_id,
                    message=(
                        f"binding 'automatic' ({binding.automatic}) does not match the "
                        f"{template.executor.value} executor of template '{template.id}'"
                    ),
                )
            )
        findings += _check_template_interface(schema, node_id, binding, template)
    return findings


def _check_template_interface(
    schema: ProcessSchema,
    node_id: str,
    binding: ServiceBinding,
    template: ActivityTemplate,
) -> list[ValidationFinding]:
    """A3: the parameter mapping conforms to the template interface."""

    findings: list[ValidationFinding] = []
    parameters = {p.name: p for p in [*template.inputs, *template.outputs]}
    for param in parameters.values():
        if param.mandatory and param.name not in binding.parameter_mapping:
            findings.append(
                ValidationFinding(
                    rule="A3",
                    node_id=node_id,
                    message=f"mandatory parameter '{param.name}' is not bound",
                )
            )
    for param_name, element_id in binding.parameter_mapping.items():
        mapped_param = parameters.get(param_name)
        if mapped_param is None:
            findings.append(
                ValidationFinding(
                    rule="A3",
                    node_id=node_id,
                    message=f"template '{template.id}' has no parameter '{param_name}'",
                )
            )
            continue
        element = schema.data_elements.get(element_id)
        if element is None:
            findings.append(
                ValidationFinding(
                    rule="A3",
                    node_id=node_id,
                    message=f"parameter '{param_name}' is bound to unknown element '{element_id}'",
                )
            )
        elif element.data_type is not mapped_param.data_type:
            findings.append(
                ValidationFinding(
                    rule="A3",
                    node_id=node_id,
                    message=(
                        f"parameter '{param_name}' ({mapped_param.data_type.value}) does not match "
                        f"element '{element_id}' ({element.data_type.value})"
                    ),
                )
            )
    return findings


# --- I1-I4: integration bindings (automatic, tool-driven services) -------


def _check_integration(schema: ProcessSchema) -> list[ValidationFinding]:
    """Integration rules I1-I4 for automatic, tool-driven service bindings.

    Silent unless a service binding sets ``automation`` to something other than
    ``MANUAL_NONE`` -- a model without integration bindings produces no
    findings, so the group is fully additive (like the temporal group).

    * I1: the binding is well-formed -- ``EXTERNAL_TASK`` needs a non-empty
      topic, ``HTTP_PUSH`` a non-empty endpoint reference.
    * I2: automation is consistent -- an automated binding is marked
      ``automatic`` and carries exactly one execution pattern (topic XOR
      endpoint). The "no interactive staff rule" half is covered by Z4.
    * I3: every parameter-mapping target references an existing data element
      (the deeper written-before/type guarantees stay with D1/D3/A3).
    * I4: the model carries no inline secrets -- topic/endpoint_ref are bare
      references, never a credential-bearing URL.
    """

    findings: list[ValidationFinding] = []
    for node_id, binding in schema.service_bindings.items():
        if binding.automation is AutomationKind.MANUAL_NONE:
            continue
        findings += _check_integration_binding(schema, node_id, binding)
    return findings


def _check_integration_binding(
    schema: ProcessSchema, node_id: str, binding: ServiceBinding
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    kind = binding.automation

    # I1 + I2: the right execution pattern is present and is the only one.
    if kind is AutomationKind.EXTERNAL_TASK:
        if not (binding.topic or "").strip():
            findings.append(
                ValidationFinding(
                    rule="I1",
                    node_id=node_id,
                    message="EXTERNAL_TASK binding requires a non-empty topic",
                )
            )
        if binding.endpoint_ref is not None:
            findings.append(
                ValidationFinding(
                    rule="I2",
                    node_id=node_id,
                    message="EXTERNAL_TASK binding must not set an endpoint_ref",
                )
            )
    elif kind is AutomationKind.HTTP_PUSH:
        if not (binding.endpoint_ref or "").strip():
            findings.append(
                ValidationFinding(
                    rule="I1",
                    node_id=node_id,
                    message="HTTP_PUSH binding requires a non-empty endpoint_ref",
                )
            )
        if binding.topic is not None:
            findings.append(
                ValidationFinding(
                    rule="I2",
                    node_id=node_id,
                    message="HTTP_PUSH binding must not set a topic",
                )
            )

    # I2: an automated binding is also flagged automatic (never interactive).
    if not binding.automatic:
        findings.append(
            ValidationFinding(
                rule="I2",
                node_id=node_id,
                message="automated binding must be marked automatic",
            )
        )

    # I3: parameter-mapping targets must reference existing data elements.
    for param, element_id in binding.parameter_mapping.items():
        if element_id not in schema.data_elements:
            findings.append(
                ValidationFinding(
                    rule="I3",
                    node_id=node_id,
                    message=(
                        f"parameter '{param}' maps to unknown data element "
                        f"'{element_id}'"
                    ),
                )
            )

    # I4: no inline secrets -- topic/endpoint_ref are bare references.
    findings += _check_no_inline_secret(node_id, "topic", binding.topic)
    findings += _check_no_inline_secret(
        node_id, "endpoint_ref", binding.endpoint_ref
    )
    return findings


def _check_no_inline_secret(
    node_id: str, field: str, value: str | None
) -> list[ValidationFinding]:
    """I4: a reference field must not embed a URL scheme or credentials."""

    if value is None:
        return []
    if "://" in value or "@" in value:
        return [
            ValidationFinding(
                rule="I4",
                node_id=node_id,
                message=(
                    f"{field} must be a bare reference without an inline URL or "
                    f"credentials"
                ),
            )
        ]
    return []


def _check_t3_escalations(schema: ProcessSchema) -> list[ValidationFinding]:
    """T3: every modelled overdue reaction is well-formed and decidable.

    Fully additive -- a schema without escalation policies produces no
    findings (Eskalations-Konzept §3):

    - T3a: the policy sits on an existing, *interactive* ACTIVITY with a
      resolvable target time (otherwise the due instant -- and thus every
      stage's trigger -- would be undefined).
    - T3b: at least one stage; offsets >= 0 and strictly ascending.
    - T3c: every stage rule is structurally well-formed (Z1-checked), free of
      node-referencing kinds (an escalation targets a role/unit, never a
      relative performer) and resolves to at least one possible agent
      (Z2-analog over the design-time over-approximation).
    """

    findings: list[ValidationFinding] = []

    def fail(message: str, node_id: str) -> None:
        findings.append(ValidationFinding(rule="T3", node_id=node_id, message=message))

    for node_id, policy in schema.escalation_policies.items():
        node = schema.nodes.get(node_id)
        if node is None or node.type is not NodeType.ACTIVITY:
            fail("escalation policy must sit on an ACTIVITY node (T3a)", node_id)
            continue
        binding = schema.service_bindings.get(node_id)
        if binding is not None and binding.automatic:
            fail(
                "an automatic activity cannot carry an escalation policy "
                "(incidents/retries cover machines; T3a)",
                node_id,
            )
        if target_seconds(schema.time_constraints.get(node_id)) is None:
            fail(
                "escalation requires a resolvable target time on the node "
                "(target_lead_seconds or max_duration_seconds; T3a)",
                node_id,
            )
        if not policy.stages:
            fail("escalation policy needs at least one stage (T3b)", node_id)
        previous: float | None = None
        for stage in policy.stages:
            if stage.after_seconds < 0:
                fail("stage offset must be >= 0 seconds (T3b)", node_id)
            if previous is not None and stage.after_seconds <= previous:
                fail("stage offsets must be strictly ascending (T3b)", node_id)
            previous = stage.after_seconds
            findings += _check_staff_rule_node(schema, node_id, stage.rule)
            if _contains_node_ref(stage.rule):
                fail(
                    "escalation targets must not use node-referencing staff "
                    "rule kinds (T3c)",
                    node_id,
                )
                continue
            possible = _possible_agents(schema.org_model, stage.rule)
            if possible is not None and not possible:
                fail(
                    "escalation target cannot resolve to any agent in the "
                    "org model (T3c)",
                    node_id,
                )
    return findings


def _contains_node_ref(rule: StaffRule) -> bool:
    """True when a rule tree uses a node-referencing kind (forbidden in T3c)."""

    if rule.kind in STAFF_NODE_REF_KINDS:
        return True
    return any(_contains_node_ref(op) for op in rule.operands)


def _check_z2_resolvable(schema: ProcessSchema) -> list[ValidationFinding]:
    """Z2: each staff rule can potentially resolve to at least one agent."""

    findings: list[ValidationFinding] = []
    for node_id, rule in schema.staff_rules.items():
        possible = _possible_agents(schema.org_model, rule)
        if possible is not None and not possible:
            findings.append(
                ValidationFinding(
                    rule="Z2",
                    node_id=node_id,
                    message="staff rule cannot resolve to any agent in the org model",
                )
            )
    return findings


def _possible_agents(org: OrgModel, rule: StaffRule) -> set[str] | None:
    """Over-approximation of the agents a rule could resolve to (for Z2).

    Returns ``None`` for the unbounded 'universe' (a NodePerformingAgent is
    bound to some agent at runtime, so it is always potentially non-empty,
    even against an empty org model). A concrete empty set means the rule is
    definitely unsatisfiable. EXCEPT only removes, so its upper bound is the
    left operand's possible set.
    """

    if rule.kind is StaffRuleKind.ROLE:
        return {a.id for a in org.agents.values() if rule.ref in a.role_ids}
    if rule.kind is StaffRuleKind.ORG_UNIT:
        units = {rule.ref} | _descendant_units(org, rule.ref, rule.recursive)
        return {a.id for a in org.agents.values() if a.org_unit_id in units}
    if rule.kind is StaffRuleKind.AGENT:
        # A single named agent: the bound is exactly that agent (empty if the
        # agent is unknown, which also makes Z2 flag it as unsatisfiable).
        return {rule.ref} if rule.ref in org.agents else set()
    if rule.kind is StaffRuleKind.NODE_PERFORMING_AGENT:
        return None  # universe: resolved at runtime
    if rule.kind is StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR:
        # The resolved supervisor is always the manager of *some* org unit, so
        # the set of all org-unit managers is a safe (bounded) over-approximation.
        # An empty bound (no unit has a manager) means the rule can never resolve
        # -> Z2 rejects it; a bounded set also lets N3 check every recipient.
        return {
            u.manager_id for u in org.org_units.values() if u.manager_id is not None
        }
    operand_sets = [_possible_agents(org, op) for op in rule.operands]
    if rule.kind is StaffRuleKind.AND:
        return _intersect_bounds(operand_sets)
    if rule.kind is StaffRuleKind.OR:
        return _union_bounds(operand_sets)
    # EXCEPT: upper bound is the left operand (removing agents cannot add any).
    return operand_sets[0]


def _intersect_bounds(bounds: list[set[str] | None]) -> set[str] | None:
    result: set[str] | None = None  # None == universe
    for bound in bounds:
        if bound is None:
            continue
        result = bound if result is None else (result & bound)
    return result


def _union_bounds(bounds: list[set[str] | None]) -> set[str] | None:
    result: set[str] = set()
    for bound in bounds:
        if bound is None:
            return None  # union with universe is universe
        result |= bound
    return result


def _descendant_units(org: OrgModel, unit_id: str | None, recursive: bool) -> set[str]:
    if not recursive or unit_id is None:
        return set()
    descendants: set[str] = set()
    frontier = [unit_id]
    while frontier:
        current = frontier.pop()
        for uid, unit in org.org_units.items():
            if unit.parent_id == current and uid not in descendants:
                descendants.add(uid)
                frontier.append(uid)
    return descendants


def _check_z3_backrefs(schema: ProcessSchema) -> list[ValidationFinding]:
    """Z3: NodePerformingAgent refs must be guaranteed-executed before the node."""

    findings: list[ValidationFinding] = []
    before = _must_executed_before(schema)
    for node_id, rule in schema.staff_rules.items():
        for ref in _node_refs(rule):
            if ref not in before.get(node_id, set()):
                findings.append(
                    ValidationFinding(
                        rule="Z3",
                        node_id=node_id,
                        message=(
                            f"NodePerformingAgent('{ref}') is not guaranteed to run "
                            f"before this node on all paths"
                        ),
                    )
                )
    return findings


def _node_refs(rule: StaffRule) -> set[str]:
    if rule.kind in STAFF_NODE_REF_KINDS and rule.ref is not None:
        return {rule.ref}
    refs: set[str] = set()
    for operand in rule.operands:
        refs |= _node_refs(operand)
    return refs


# --- N1-N4: modelled e-mail notification ---------------------------------


def _check_mail(schema: ProcessSchema) -> list[ValidationFinding]:
    """Rule group N -- correctness of modelled e-mail notifications.

    Silent unless the org model carries addresses or a node carries a
    ``MailBinding`` (fully additive, like the temporal group). Enforces:

    * N1 -- every address in the org master data is well-formed;
    * N2 -- a mail binding sits on an ACTIVITY that carries a staff rule (BZR);
    * N3 -- for the binding's mode, *every* address that could ever be needed
      exists (per-agent: every possibly-eligible agent incl. deputies has an
      ``email``; group: every addressed role/unit has a ``mailbox``);
    * N4 -- every ``{element_id}`` placeholder in subject/body refers to an
      INSTANCE data element guaranteed written before the node.

    N3 is the correctness heart of the feature: because it runs before every
    commit -- and the API re-runs the whole validator for every schema that
    references a shared org model on each org edit -- a notification can never
    reach a state in which a possible recipient has no address.
    """

    findings = _check_n1_addresses(schema.org_model)
    if not schema.mail_bindings:
        return findings
    # ``_must_written_before`` needs an intact control graph (like D1/D2); when
    # the structure is broken we still check placeholder existence/scope (N4) but
    # skip the "guaranteed written" part until the structure is fixed.
    before = None if _structure_broken(schema) else _must_written_before(schema)
    for node_id, binding in schema.mail_bindings.items():
        findings += _check_mail_binding(schema, node_id, binding, before)
    return findings


def _check_n1_addresses(org: OrgModel) -> list[ValidationFinding]:
    """N1: every address set in the org master data is syntactically valid."""

    findings: list[ValidationFinding] = []
    for agent in org.agents.values():
        if agent.email is not None and not is_valid_email(agent.email):
            findings.append(
                ValidationFinding(
                    rule="N1",
                    message=f"agent '{agent.id}' has a malformed e-mail address",
                )
            )
    for role in org.roles.values():
        if role.mailbox is not None and not is_valid_email(role.mailbox):
            findings.append(
                ValidationFinding(
                    rule="N1",
                    message=f"role '{role.id}' has a malformed group mailbox",
                )
            )
    for unit in org.org_units.values():
        if unit.mailbox is not None and not is_valid_email(unit.mailbox):
            findings.append(
                ValidationFinding(
                    rule="N1",
                    message=f"org unit '{unit.id}' has a malformed mailbox",
                )
            )
    return findings


def _check_mail_binding(
    schema: ProcessSchema,
    node_id: str,
    binding: MailBinding,
    before: dict[str, set[str]] | None,
) -> list[ValidationFinding]:
    """N2-N4 for a single mail binding."""

    # N2: the binding must sit on an ACTIVITY that has a staff rule -- only an
    # interactive step has an assignee to notify.
    node = schema.nodes.get(node_id)
    if node is None:
        return [
            ValidationFinding(
                rule="N2", node_id=node_id, message="mail binding on unknown node"
            )
        ]
    if node.type is not NodeType.ACTIVITY:
        return [
            ValidationFinding(
                rule="N2",
                node_id=node_id,
                message="mail notifications are only allowed on ACTIVITY nodes",
            )
        ]
    rule = schema.staff_rules.get(node_id)
    if rule is None:
        return [
            ValidationFinding(
                rule="N2",
                node_id=node_id,
                message=(
                    "mail notification requires a staff rule (BZR) on the node -- "
                    "there is no assignee to address"
                ),
            )
        ]
    policy = schema.escalation_policies.get(node_id)
    functional_rules = [
        stage.rule
        for stage in (policy.stages if policy is not None else [])
        if stage.kind is EscalationKind.FUNCTIONAL
    ]
    findings = _check_n3_addresses(
        schema.org_model, node_id, binding, rule, extra_rules=functional_rules
    )
    findings += _check_n4_template(schema, node_id, binding, before)
    return findings


def _check_n3_addresses(
    org: OrgModel,
    node_id: str,
    binding: MailBinding,
    rule: StaffRule,
    *,
    extra_rules: Sequence[StaffRule] = (),
) -> list[ValidationFinding]:
    """N3: every address the binding could ever need is present in the org.

    ``extra_rules`` are additional *possible performer* sets beyond the staff
    rule -- today the FUNCTIONAL escalation stage targets (T3/E9): any stage
    may fire, so its agents are possible recipients of the task notification
    and must be addressable too.
    """

    findings: list[ValidationFinding] = []
    if binding.mode is MailRecipientMode.TO_ELIGIBLE_AGENTS:
        possible = _possible_agents(org, rule)
        for extra in extra_rules:
            extra_possible = _possible_agents(org, extra)
            if possible is not None and extra_possible is not None:
                possible = set(possible) | extra_possible
        if possible is None:
            # The rule depends on a prior node's performer (universe); the
            # recipient set is not statically bounded, so we cannot guarantee
            # every recipient has an address. CbC therefore forbids per-agent
            # notification here (the group-mailbox mode stays available).
            return [
                ValidationFinding(
                    rule="N3",
                    node_id=node_id,
                    message=(
                        "recipient set is not statically determinable (the staff "
                        "rule depends on a prior node's performer); a per-agent mail "
                        "notification cannot be modelled here -- use a group mailbox"
                    ),
                )
            ]
        recipients = _with_deputies(org, possible) if binding.include_deputies else possible
        for agent_id in sorted(recipients):
            agent = org.agents.get(agent_id)
            if agent is None or not (agent.email or "").strip():
                who = agent.name if agent is not None else agent_id
                findings.append(
                    ValidationFinding(
                        rule="N3",
                        node_id=node_id,
                        message=f"possible assignee '{who}' has no e-mail address",
                    )
                )
        return findings

    # TO_GROUP_MAILBOX: every addressed role/unit must carry a mailbox.
    groups = _group_refs(rule)
    if not groups:
        findings.append(
            ValidationFinding(
                rule="N3",
                node_id=node_id,
                message=(
                    "staff rule addresses no role or org unit, so there is no group "
                    "mailbox to notify"
                ),
            )
        )
    for kind, ref in groups:
        if kind is StaffRuleKind.ROLE:
            role = org.roles.get(ref)
            if role is None or not (role.mailbox or "").strip():
                findings.append(
                    ValidationFinding(
                        rule="N3", node_id=node_id, message=f"role '{ref}' has no group mailbox"
                    )
                )
        else:
            unit = org.org_units.get(ref)
            if unit is None or not (unit.mailbox or "").strip():
                findings.append(
                    ValidationFinding(
                        rule="N3", node_id=node_id, message=f"org unit '{ref}' has no mailbox"
                    )
                )
    if _node_refs(rule):
        findings.append(
            ValidationFinding(
                rule="N3",
                node_id=node_id,
                message=(
                    "staff rule includes a prior-node performer, which has no group "
                    "mailbox; use the per-agent mode for it"
                ),
            )
        )
    return findings


def _check_n4_template(
    schema: ProcessSchema,
    node_id: str,
    binding: MailBinding,
    before: dict[str, set[str]] | None,
) -> list[ValidationFinding]:
    """N4: every template placeholder resolves to an available INSTANCE element."""

    findings: list[ValidationFinding] = []
    available = None if before is None else before.get(node_id, set())
    for field, text in (("subject", binding.subject), ("body", binding.body)):
        for ref in template_placeholders(text):
            element = schema.data_elements.get(ref)
            if element is None:
                findings.append(
                    ValidationFinding(
                        rule="N4",
                        node_id=node_id,
                        message=(
                            f"{field} placeholder '{{{ref}}}' refers to unknown data "
                            f"element '{ref}'"
                        ),
                    )
                )
                continue
            if element.source is not DataSourceKind.INSTANCE:
                findings.append(
                    ValidationFinding(
                        rule="N4",
                        node_id=node_id,
                        message=(
                            f"{field} placeholder '{{{ref}}}' refers to a non-INSTANCE data "
                            f"element that is not available in the mail text"
                        ),
                    )
                )
                continue
            if available is not None and ref not in available:
                findings.append(
                    ValidationFinding(
                        rule="N4",
                        node_id=node_id,
                        message=(
                            f"{field} placeholder '{{{ref}}}' is not guaranteed to be set "
                            f"when this task becomes ready"
                        ),
                    )
                )
    return findings


def _with_deputies(org: OrgModel, base: set[str]) -> set[str]:
    """Extend an agent set by deputies, following the chain transitively (N3).

    Mirrors the runtime resolution in :mod:`procworks.assignment`: whenever an
    agent is eligible, so is its deputy. Kept local so the validator stays
    self-contained (like ``_descendant_units``).
    """

    result = set(base)
    frontier = list(base)
    while frontier:
        agent = org.agents.get(frontier.pop())
        if agent is None or agent.deputy_id is None:
            continue
        if agent.deputy_id not in result:
            result.add(agent.deputy_id)
            frontier.append(agent.deputy_id)
    return result


def _group_refs(
    rule: StaffRule, *, positive: bool = True
) -> list[tuple[StaffRuleKind, str]]:
    """Collect the (kind, ref) of every role/unit the rule *positively* addresses.

    Group-mailbox notification targets named groups. An ``EXCEPT`` right operand
    subtracts agents, so those groups are *not* notified -- they are skipped.
    Duplicates are removed while preserving order.
    """

    refs: list[tuple[StaffRuleKind, str]] = []
    if rule.kind in (StaffRuleKind.ROLE, StaffRuleKind.ORG_UNIT) and rule.ref is not None:
        if positive:
            refs.append((rule.kind, rule.ref))
    elif rule.kind is StaffRuleKind.EXCEPT:
        if rule.operands:
            refs += _group_refs(rule.operands[0], positive=positive)
        for operand in rule.operands[1:]:
            refs += _group_refs(operand, positive=False)
    else:  # AND / OR
        for operand in rule.operands:
            refs += _group_refs(operand, positive=positive)
    seen: dict[tuple[StaffRuleKind, str], None] = {}
    for item in refs:
        seen.setdefault(item, None)
    return list(seen)


# --- H1-H4 / F1-F3: composition (sub- and follow-up processes) -----------


def _check_composition(
    schema: ProcessSchema, resolver: SchemaResolver | None
) -> list[ValidationFinding]:
    """Run the cross-schema composition rules H1-H4 (sub-processes) and
    F1-F3 (follow-up processes)."""

    findings: list[ValidationFinding] = []
    findings += _check_subprocesses(schema, resolver)
    findings += _check_follow_ups(schema, resolver)
    return findings


def _check_subprocesses(
    schema: ProcessSchema, resolver: SchemaResolver | None
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    # Computed once for all bindings; ``None`` on a structurally broken schema,
    # where the must-analysis carries no meaning (same guard as D1/D2).
    written_before = (
        None
        if not schema.sub_process_bindings or _structure_broken(schema)
        else _must_written_before(schema)
    )

    # Every SUBPROCESS node must carry a binding, and every binding must point
    # at an existing SUBPROCESS node.
    for node in schema.nodes.values():
        if node.type is NodeType.SUBPROCESS and node.id not in schema.sub_process_bindings:
            findings.append(
                ValidationFinding(
                    rule="H1",
                    node_id=node.id,
                    message="SUBPROCESS node has no sub-process binding",
                )
            )
    for node_id, binding in schema.sub_process_bindings.items():
        bound_node = schema.nodes.get(node_id)
        if bound_node is None or bound_node.type is not NodeType.SUBPROCESS:
            findings.append(
                ValidationFinding(
                    rule="H1",
                    node_id=node_id,
                    message="sub-process binding does not reference a SUBPROCESS node",
                )
            )
            continue
        # H2 (local part): mapped parent elements must exist.
        for parent_eid in (*binding.input_mapping.values(), *binding.output_mapping.values()):
            if parent_eid not in schema.data_elements:
                findings.append(
                    ValidationFinding(
                        rule="H2",
                        node_id=node_id,
                        message=f"mapping references unknown parent data element '{parent_eid}'",
                    )
                )
        # H2 (local part): a mapped INPUT is copied into the child when it
        # starts, so the parent element must already hold a value there. This
        # lives in the *resolver-free* part on purpose: it needs only parent-side
        # information, and an operation that runs without a resolver (delete_node
        # removing the writer, for instance) must not be able to break it behind
        # the composition rules' back.
        findings += _check_subprocess_inputs_supplied(schema, node_id, binding, written_before)
        if resolver is None:
            continue
        findings += _check_subprocess_target(schema, node_id, binding, resolver)

    if resolver is not None and _has_subprocess_cycle(schema, resolver):
        findings.append(
            ValidationFinding(
                rule="H3",
                message="sub-process hierarchy is cyclic (a process cannot contain itself)",
            )
        )
    return findings


def _check_subprocess_target(
    schema: ProcessSchema,
    node_id: str,
    binding: SubProcessBinding,
    resolver: SchemaResolver,
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    target = resolver(binding.target_schema_id, binding.target_version)
    if target is None:
        findings.append(
            ValidationFinding(
                rule="H1",
                node_id=node_id,
                message=(
                    f"sub-process target '{binding.target_schema_id}' "
                    f"v{binding.target_version} not found"
                ),
            )
        )
        return findings
    if target.lifecycle_state is not LifecycleState.RELEASED:
        findings.append(
            ValidationFinding(
                rule="H1",
                node_id=node_id,
                message=(
                    f"sub-process target '{binding.target_schema_id}' is "
                    f"{target.lifecycle_state.value}, must be RELEASED"
                ),
            )
        )
    # H2 (type conformance): each mapped target element must exist and match.
    mappings = (
        ("input", binding.input_mapping),
        ("output", binding.output_mapping),
    )
    for kind, mapping in mappings:
        for target_eid, parent_eid in mapping.items():
            target_el = target.data_elements.get(target_eid)
            parent_el = schema.data_elements.get(parent_eid)
            if target_el is None:
                findings.append(
                    ValidationFinding(
                        rule="H2",
                        node_id=node_id,
                        message=f"{kind} maps unknown target element '{target_eid}'",
                    )
                )
                continue
            if parent_el is not None and target_el.data_type is not parent_el.data_type:
                findings.append(
                    ValidationFinding(
                        rule="H2",
                        node_id=node_id,
                        message=(
                            f"{kind} type mismatch: target '{target_eid}' is "
                            f"{target_el.data_type.value}, parent '{parent_eid}' is "
                            f"{parent_el.data_type.value}"
                        ),
                    )
                )
    # H2 (data-passing soundness): a mapped OUTPUT is written back into the
    # parent and may be read downstream, so the child must guarantee to produce
    # it on every path. Otherwise the whole model would not be runnable.
    if binding.output_mapping:
        guaranteed = _must_written_before(target).get(target.end_node().id, set())
        for target_eid, parent_eid in binding.output_mapping.items():
            if target_eid in target.data_elements and target_eid not in guaranteed:
                findings.append(
                    ValidationFinding(
                        rule="H2",
                        node_id=node_id,
                        message=(
                            f"output '{target_eid}' is not written on every path of "
                            f"sub-process '{binding.target_schema_id}', so parent "
                            f"element '{parent_eid}' would be undefined"
                        ),
                    )
                )
    return findings


def _check_subprocess_inputs_supplied(
    schema: ProcessSchema,
    node_id: str,
    binding: SubProcessBinding,
    written_before: dict[str, set[str]] | None,
) -> list[ValidationFinding]:
    """H2 (parent side): every mapped input holds a value when the child starts.

    The mirror image of the output guarantee: the child must produce each mapped
    output on every path, and the parent must supply each mapped input before
    the call. Without this the child begins with a missing input and the failure
    surfaces at runtime inside a *different* schema than the one carrying the
    modelling mistake (concept §3.6 H2 -- "Jeder Pflicht-Input des Sub-Prozesses
    ist aus einem geschriebenen Datenelement des Hauptprozesses versorgt").

    Needs only parent-side information, so it deliberately runs **without** a
    resolver: an operation that validates resolver-free (``delete_node``
    removing the writing step, say) must not be able to break it unnoticed.
    """

    if written_before is None or not binding.input_mapping:
        return []
    findings: list[ValidationFinding] = []
    supplied = written_before.get(node_id, set())
    for target_eid, parent_eid in sorted(binding.input_mapping.items()):
        parent_el = schema.data_elements.get(parent_eid)
        if parent_el is None or parent_eid in supplied:
            continue  # unknown element is reported by the local existence check
        if _connector_supplied(parent_el):
            continue  # resolved by the connector, not by a process write
        findings.append(
            ValidationFinding(
                rule="H2",
                node_id=node_id,
                message=(
                    f"input '{target_eid}' is mapped from parent element "
                    f"'{parent_el.name}', which is not guaranteed to be written "
                    f"on every path to this sub-process"
                ),
            )
        )
    return findings


def _has_subprocess_cycle(schema: ProcessSchema, resolver: SchemaResolver) -> bool:
    """True if the transitive sub-process call graph leads back to ``schema``."""

    visited: set[str] = set()

    def visit(target_id: str, version: int | None) -> bool:
        if target_id == schema.id:
            return True
        key = f"{target_id}:{version}"
        if key in visited:
            return False
        visited.add(key)
        target = resolver(target_id, version)
        if target is None:
            return False
        for binding in target.sub_process_bindings.values():
            if visit(binding.target_schema_id, binding.target_version):
                return True
        return False

    return any(
        visit(b.target_schema_id, b.target_version)
        for b in schema.sub_process_bindings.values()
    )


def _check_follow_up_condition(
    schema: ProcessSchema, link_id: str, condition: str | None
) -> list[ValidationFinding]:
    """F4: a CONDITIONAL follow-up's predicate is parseable and only reads
    existing data elements."""

    if condition is None or not condition.strip():
        return [
            ValidationFinding(
                rule="F4",
                message=f"conditional follow-up '{link_id}' has no condition",
            )
        ]
    try:
        names = referenced_names(condition)
    except ConditionError as exc:
        return [
            ValidationFinding(
                rule="F4",
                message=f"follow-up '{link_id}' has an invalid condition: {exc}",
            )
        ]
    findings: list[ValidationFinding] = []
    for name in sorted(names):
        if name not in schema.data_elements:
            findings.append(
                ValidationFinding(
                    rule="F4",
                    message=(
                        f"follow-up '{link_id}' condition references unknown data "
                        f"element '{name}'"
                    ),
                )
            )
    if findings or _structure_broken(schema):
        return findings

    # F4 (evaluability): the predicate runs when the instance *finishes*, so
    # every element it reads must be written on **every** path to END. An
    # unwritten one makes the evaluator raise -- and because that happens while
    # completing the final activity, the instance can then never be completed at
    # all (the same dead end K1 produced, reached by a different route). This is
    # the coupling that makes "die Auswertung beim Instanzabschluss ist
    # garantiert definiert" (concept §3.6 F4) actually true.
    supplied = _must_written_before(schema).get(schema.end_node().id, set())
    for name in sorted(names):
        element = schema.data_elements[name]
        if name in supplied or _connector_supplied(element):
            continue
        findings.append(
            ValidationFinding(
                rule="F4",
                message=(
                    f"follow-up '{link_id}' condition reads '{element.name}', which "
                    f"is not written on every path to the end of the process"
                ),
            )
        )
    return findings


def _check_follow_ups(
    schema: ProcessSchema, resolver: SchemaResolver | None
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    for link in schema.follow_up_links:
        # F2 (local part): mapped source elements must exist.
        for source_eid in link.handover_mapping.values():
            if source_eid not in schema.data_elements:
                findings.append(
                    ValidationFinding(
                        rule="F2",
                        message=(
                            f"follow-up '{link.id}' handover references unknown source "
                            f"element '{source_eid}'"
                        ),
                    )
                )
        # F4: a CONDITIONAL trigger needs a well-formed condition that only
        # reads existing data elements (so it can be evaluated deterministically
        # against an instance's data values at runtime).
        if link.trigger is FollowUpTrigger.CONDITIONAL:
            findings += _check_follow_up_condition(schema, link.id, link.condition)
        if resolver is None:
            continue
        target = resolver(link.target_schema_id, link.target_version)
        if target is None:
            findings.append(
                ValidationFinding(
                    rule="F1",
                    message=(
                        f"follow-up target '{link.target_schema_id}' has no "
                        f"matching released version"
                    ),
                )
            )
            continue
        if target.lifecycle_state is not LifecycleState.RELEASED:
            findings.append(
                ValidationFinding(
                    rule="F1",
                    message=(
                        f"follow-up target '{link.target_schema_id}' is "
                        f"{target.lifecycle_state.value}, must be RELEASED"
                    ),
                )
            )
        # F2 (type conformance): each mapped target start element must match.
        for target_eid, source_eid in link.handover_mapping.items():
            target_el = target.data_elements.get(target_eid)
            source_el = schema.data_elements.get(source_eid)
            if target_el is None:
                findings.append(
                    ValidationFinding(
                        rule="F2",
                        message=(
                            f"follow-up '{link.id}' handover maps unknown target "
                            f"element '{target_eid}'"
                        ),
                    )
                )
                continue
            if source_el is not None and target_el.data_type is not source_el.data_type:
                findings.append(
                    ValidationFinding(
                        rule="F2",
                        message=(
                            f"follow-up '{link.id}' type mismatch: target "
                            f"'{target_eid}' is {target_el.data_type.value}, source "
                            f"'{source_eid}' is {source_el.data_type.value}"
                        ),
                    )
                )
    return findings


# --- T1-T2: temporal perspective (roadmap E5, additive) ------------------


def _check_temporal(schema: ProcessSchema) -> list[ValidationFinding]:
    """Static time-consistency rules T1 (well-formed) and T2 (critical path).

    These rules only fire when the schema carries temporal annotations
    (``time_constraints`` and/or ``deadline_seconds``); a model without time
    data produces no findings, so the check is fully additive.

    * T1: every annotated duration and the deadline are non-negative and refer
      to an existing node.
    * T2: the critical path (longest accumulated duration from START to END)
      must not exceed the schema deadline. Parallel/alternative branches are
      treated by their longest branch (worst case), so the bound is sound.
    """

    if not schema.time_constraints and schema.deadline_seconds is None:
        return []

    findings: list[ValidationFinding] = []

    # T1: well-formedness of the annotations.
    if schema.deadline_seconds is not None and schema.deadline_seconds < 0:
        findings.append(
            ValidationFinding(
                rule="T1",
                message=(
                    f"deadline_seconds must be >= 0, got {schema.deadline_seconds}"
                ),
            )
        )
    for node_id, constraint in schema.time_constraints.items():
        if node_id not in schema.nodes:
            findings.append(
                ValidationFinding(
                    rule="T1",
                    message=f"time constraint references unknown node '{node_id}'",
                    node_id=node_id,
                )
            )
            continue
        duration = constraint.max_duration_seconds
        if duration is not None and duration < 0:
            findings.append(
                ValidationFinding(
                    rule="T1",
                    message=(
                        f"max_duration_seconds of '{node_id}' must be >= 0, "
                        f"got {duration}"
                    ),
                    node_id=node_id,
                )
            )
        lead = constraint.target_lead_seconds
        if lead is not None and lead < 0:
            findings.append(
                ValidationFinding(
                    rule="T1",
                    message=(
                        f"target_lead_seconds of '{node_id}' must be >= 0, "
                        f"got {lead}"
                    ),
                    node_id=node_id,
                )
            )

    # T2: the critical path must fit the deadline (only when a deadline exists
    # and the annotations so far are well-formed).
    if schema.deadline_seconds is not None and not findings:
        critical = _critical_path_seconds(schema)
        if critical is not None and critical > schema.deadline_seconds:
            findings.append(
                ValidationFinding(
                    rule="T2",
                    message=(
                        f"critical path of {critical:g}s exceeds the deadline of "
                        f"{schema.deadline_seconds:g}s"
                    ),
                )
            )
    return findings


def _critical_path_seconds(schema: ProcessSchema) -> float | None:
    """Longest accumulated max-duration from START to END, or ``None``.

    Returns ``None`` if the control graph is not a well-formed DAG (e.g. during
    incremental construction); the structural rules cover those cases instead.

    Loop-aware (stage S3): a loop whose decision carries ``max_iterations``
    charges its body ``max_iterations`` times -- the extra passes are folded
    into the LOOP_END's duration (:func:`_loop_time_extras`), so the plain
    one-pass DAG walk below stays correct. A loop without the bound keeps the
    documented one-pass approximation (a deadline over such a loop is a
    promise for the run without repetition, Schleifen-Konzept §7).
    """

    nodes = schema.nodes
    if not nodes:
        return None
    indegree: dict[str, int] = {nid: 0 for nid in nodes}
    succ: dict[str, list[str]] = {nid: [] for nid in nodes}
    for edge in schema.edges:
        # T2 stays a control-flow bound: SYNC waits (K4) are deliberately not
        # charged (conservative approximation, documented).
        if edge.type is not EdgeType.CONTROL:
            continue
        if edge.source in nodes and edge.target in nodes:
            succ[edge.source].append(edge.target)
            indegree[edge.target] += 1

    extras = _loop_time_extras(schema)

    def duration(node_id: str) -> float:
        constraint = schema.time_constraints.get(node_id)
        base = 0.0
        if constraint is not None and constraint.max_duration_seconds is not None:
            base = constraint.max_duration_seconds
        return base + extras.get(node_id, 0.0)

    complete: dict[str, float] = {}
    queue: deque[str] = deque(nid for nid, deg in indegree.items() if deg == 0)
    visited = 0
    while queue:
        current = queue.popleft()
        visited += 1
        complete[current] = complete.get(current, 0.0) + duration(current)
        for target in succ[current]:
            complete[target] = max(complete.get(target, 0.0), complete[current])
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(nodes):  # a cycle -> not a DAG, leave to structural rules
        return None
    return max(complete.values(), default=0.0)


def _loop_time_extras(schema: ProcessSchema) -> dict[str, float]:
    """Extra seconds charged to each LOOP_END for its bounded repetitions (T2).

    For every loop whose decision carries ``max_iterations`` = m, the longest
    internal path through the block (LOOP_START to LOOP_END) is charged
    ``(m - 1)`` additional times onto the LOOP_END. Blocks are processed
    innermost-first (smaller bodies first), so a nested bounded loop's extra
    is already part of the enclosing block's internal path and multiplies
    correctly. Loops without the bound contribute nothing (one-pass
    approximation preserved).
    """

    blocks: list[tuple[int, str, str, set[str]]] = []
    for nid, node in schema.nodes.items():
        if node.type is not NodeType.LOOP_START:
            continue
        try:
            end_id, body = loop_block(schema, nid)
        except ValueError:
            continue  # unpaired start -> K6a reports it, no time charge
        blocks.append((len(body), nid, end_id, body))

    extras: dict[str, float] = {}
    for _, start_id, end_id, body in sorted(blocks, key=lambda b: b[0]):
        decision = schema.loop_decisions.get(end_id)
        if decision is None or decision.max_iterations is None:
            continue
        if decision.max_iterations < 2:
            continue  # ill-formed bound -> K6b reports it, no time charge

        def duration(node_id: str) -> float:
            constraint = schema.time_constraints.get(node_id)
            base = 0.0
            if constraint is not None and constraint.max_duration_seconds is not None:
                base = constraint.max_duration_seconds
            return base + extras.get(node_id, 0.0)

        block = body | {start_id, end_id}
        longest: dict[str, float] = {start_id: duration(start_id)}
        for node_id in _topological_order(schema):
            if node_id not in block or node_id == start_id:
                continue
            preds = [
                e.source for e in schema.incoming(node_id) if e.source in block
            ]
            best = max(
                (longest.get(p, 0.0) for p in preds), default=0.0
            )
            longest[node_id] = best + duration(node_id)
        extras[end_id] = extras.get(end_id, 0.0) + (
            decision.max_iterations - 1
        ) * longest.get(end_id, 0.0)
    return extras
