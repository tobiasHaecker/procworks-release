# SPDX-License-Identifier: BUSL-1.1
"""Schema evolution and instance migration (roadmap step 11, M1-M5).

When a new schema revision is released, each running instance is checked
*individually* against the migration criteria before it may switch versions:

  * M1  target is itself a correct, RELEASED (executable) schema -- up to old
        findings the runtime is protected against
        (:data:`RUNTIME_SAFE_OLD_FINDINGS`);
  * M2  the executed region (COMPLETED/RUNNING/SKIPPED nodes and the control
        edges among them) is preserved in the target -- the past stays
        producible;
  * M3  the markings map cleanly: no completed node is rewired/reactivated and
        every RUNNING node stays executable;
  * M4  mandatory data needed for the executed region is present (typed), and
        new mandatory data is only required ahead of the execution front;
  * M5  ad-hoc instances are not silently migrated -- an instance with ad-hoc
        deltas requires manual resolution (conservative).

An instance is *migratable* iff M1-M5 all pass. Migration is then an atomic
switch of the version assignment plus a marking/data remap; otherwise the
instance keeps running consistently on its current schema.
"""

from __future__ import annotations

from collections.abc import Iterable

from procworks.model import (
    READ_MODES,
    WRITE_MODES,
    EdgeState,
    LifecycleState,
    NodeState,
    NodeType,
    ProcessInstance,
    ProcessSchema,
)
from procworks.validator import (
    CorrectnessError,
    SchemaResolver,
    ValidationFinding,
    node_name,
    validate,
)

#: Node states that count as "already progressed" and therefore frozen.
_FROZEN = frozenset({NodeState.COMPLETED, NodeState.RUNNING, NodeState.SKIPPED})

#: Findings a released target may carry without blocking M1, by code.
#:
#: A released schema had no finding at its release (the release refuses any),
#: so a finding it has now comes from a rule that got stricter afterwards.
#: Cases already run on such a schema. Moving another case onto it is only
#: harmless where the runtime protects itself against the old value; every
#: other finding still blocks the migration (conservative):
#:
#: * ``T1.*-too-large``, ``T3.offset-too-large`` -- a time value beyond 100
#:   years: the due instant is not representable, and
#:   :func:`procworks.worklist_priority.shifted` treats it as "no due date"
#:   (logged) instead of raising; the step works, it just takes no part in
#:   the time ordering and escalation.
#: * ``U2.pattern-*`` -- an input pattern the safety check now refuses: the
#:   completion check does not run it (logged), the value is accepted
#:   unchecked, nothing hangs on the server.
#: * ``I1.backoff-too-large``, ``I1.timeout-too-large``,
#:   ``I1.retries-too-large`` -- a retry back-off beyond one day, a push
#:   timeout beyond 30 s or a retry budget above 100: the runtime caps each
#:   to its bound (:func:`procworks.integration_runtime.request_timeout_seconds`,
#:   the back-off formula, :func:`procworks.integration_runtime.retry_budget`),
#:   never raises.
#: * ``I1.negative`` -- a negative retry budget, back-off or push timeout
#:   (only from an import or a library caller before the rule): the runtime
#:   counts it as ``0`` (no retry, at once) or as the default timeout.
#: * ``F4.condition-ignored`` -- a condition on a follow-up that starts on
#:   every completion (``ON_COMPLETE``): the runtime never reads it
#:   (:func:`procworks.execution._follow_up_fires` returns before), so the
#:   follow-up starts exactly as the trigger says.
#:
#: Deliberately **not** listed: structure, data flow, staffing, composition,
#: connectors and non-finite numbers (``*-not-finite``) -- an old finding
#: there can leave a case unfinishable, without a value or in nobody's
#: worklist, and no runtime guard covers all their uses.
RUNTIME_SAFE_OLD_FINDINGS: frozenset[str] = frozenset({
    "T1.deadline-too-large",
    "T1.duration-too-large",
    "T1.lead-too-large",
    "T3.offset-too-large",
    "U2.pattern-too-long",
    "U2.pattern-invalid",
    "U2.pattern-backreference",
    "U2.pattern-too-complex",
    "I1.backoff-too-large",
    "I1.timeout-too-large",
    "I1.retries-too-large",
    "I1.negative",
    "F4.condition-ignored",
})


def _edge_key(source: str, target: str) -> str:
    return f"{source}->{target}"


def _edge_params(schema: ProcessSchema, key: str) -> dict[str, str]:
    """``{"from", "to"}`` step names of an edge key (``source->target``)."""

    source, _, target = key.partition("->")
    return {"from": node_name(schema, source), "to": node_name(schema, target)}


def _frozen_nodes(instance: ProcessInstance) -> set[str]:
    return {nid for nid, st in instance.node_states.items() if st in _FROZEN}


def _internal_edges(schema: ProcessSchema, frozen: set[str]) -> set[str]:
    """Edge keys of ``schema`` whose source *and* target are in ``frozen``.

    These are the edges inside the executed region that M2 requires to be
    identical in source and target schema. ``frozen`` is the id set from
    :func:`_frozen_nodes`; the keys use the ``source->target`` form of the
    instance's edge markings. Every edge type counts, exactly as before.
    """

    return {
        _edge_key(e.source, e.target)
        for e in schema.edges
        if e.source in frozen and e.target in frozen
    }


def draft_test_findings(
    schema: ProcessSchema, resolver: SchemaResolver | None = None
) -> list[ValidationFinding]:
    """Why a test instance of ``schema`` (a draft) must not be started, if at all.

    Since the repair path a draft may carry old findings (a rule got stricter
    after it was stored). A throw-away test instance runs on the same
    runtime as a real case, so the tolerance is the one of a migration:
    findings in :data:`RUNTIME_SAFE_OLD_FINDINGS` are harmless (the runtime
    guards against the old value); any other finding has no runtime guard --
    the test instance could hang or fail -- and refuses the start.

    :param schema: the draft to start a test instance of.
    :param resolver: as for :func:`~procworks.validator.validate`.
    :returns: one ``LC.test-incorrect`` finding (``params.rule`` and
        ``params.finding`` = rule and code of the offending finding, same
        ``node_id``) per finding that is not runtime-safe; empty when the
        test instance may start.
    """

    return [
        ValidationFinding(
            rule="LC",
            message=f"a test instance needs a model without this finding: [{f.rule}] {f.message}",
            node_id=f.node_id,
            code="LC.test-incorrect",
            params={"rule": f.rule, "finding": f.code or f.rule},
        )
        for f in validate(schema, resolver)
        if f.code not in RUNTIME_SAFE_OLD_FINDINGS
    ]


def check_migration(
    instance: ProcessInstance,
    source_schema: ProcessSchema,
    target_schema: ProcessSchema,
    *,
    resolver: SchemaResolver | None = None,
    data_mapping: dict[str, object] | None = None,
) -> list[ValidationFinding]:
    """Return the M0-M5 findings for migrating ``instance`` onto the target.

    An empty list means the instance is migratable. M0 comes first: an
    instance that already runs on the target has nothing to migrate, and the
    remaining criteria would only describe a no-op.
    """

    findings: list[ValidationFinding] = _check_m0(instance, target_schema)
    if findings:
        return findings
    findings += _check_m1(target_schema, resolver)
    # If the target is not even a correct released schema, the remaining
    # criteria cannot be assessed meaningfully.
    if findings:
        return findings
    findings += _check_m2(instance, source_schema, target_schema)
    findings += _check_m3(instance, source_schema, target_schema)
    findings += _check_m4(instance, target_schema, data_mapping)
    findings += _check_m5(instance)
    return findings


def is_migratable(
    instance: ProcessInstance,
    source_schema: ProcessSchema,
    target_schema: ProcessSchema,
    *,
    resolver: SchemaResolver | None = None,
    data_mapping: dict[str, object] | None = None,
) -> bool:
    """Convenience predicate: True iff M1-M5 all pass."""

    return not check_migration(
        instance,
        source_schema,
        target_schema,
        resolver=resolver,
        data_mapping=data_mapping,
    )


def build_migration_report(
    target_schema: ProcessSchema,
    cases: list[tuple[ProcessInstance, ProcessSchema]],
    *,
    resolver: SchemaResolver | None = None,
) -> dict[str, list[ValidationFinding]]:
    """Compute the per-instance migration report for a release inventory.

    ``cases`` pairs each active instance with its current source schema. The
    result maps instance id to its findings (empty list = migratable).
    """

    return {
        instance.id: check_migration(
            instance, source_schema, target_schema, resolver=resolver
        )
        for instance, source_schema in cases
    }


def migrate_instance(
    instance: ProcessInstance,
    source_schema: ProcessSchema,
    target_schema: ProcessSchema,
    *,
    data_mapping: dict[str, object] | None = None,
    resolver: SchemaResolver | None = None,
) -> ProcessInstance:
    """Atomically switch a migratable instance onto the target schema.

    requires: M0-M5 hold (else CorrectnessError with the findings).
    ensures:  the returned instance references the target version; its markings
              are remapped (executed region preserved, new nodes/edges start
              unmarked) and ``data_mapping`` seeds new mandatory data.
    """

    findings = check_migration(
        instance,
        source_schema,
        target_schema,
        resolver=resolver,
        data_mapping=data_mapping,
    )
    if findings:
        raise CorrectnessError(findings)

    result = instance.model_copy(deep=True)
    result.schema_id = target_schema.id
    result.schema_version = target_schema.version
    # Fresh markings for the target, then copy over the states of every element
    # that exists in both schemas (executed region matches by id).
    result.node_states = {
        nid: instance.node_states.get(nid, NodeState.NOT_ACTIVATED)
        for nid in target_schema.nodes
    }
    result.edge_states = {
        key: instance.edge_states.get(key, EdgeState.NOT_SIGNALED)
        for key in (_edge_key(e.source, e.target) for e in target_schema.edges)
    }
    if data_mapping:
        result.data_values.update(data_mapping)
    return result


# --- criteria ------------------------------------------------------------


def _check_m0(
    instance: ProcessInstance, target_schema: ProcessSchema
) -> list[ValidationFinding]:
    """M0: the instance must not already run on the target schema.

    Migrating an instance onto the revision it runs on changes nothing, yet
    it used to succeed -- and every repetition left another
    ``INSTANCE_MIGRATED`` in the history, reporting migrations that never
    happened. Refusing it here keeps every path honest (single migration, its
    dry run, the bulk assistant) without a special case at the boundary.

    Public as :func:`already_on_target` for the bulk assistant, which must
    tell this case apart from "not a candidate" with the same code.

    :param instance: the instance to migrate.
    :param target_schema: the schema it should move onto.
    :returns: one ``M0.already-on-target`` finding when ``instance`` already
        references ``target_schema`` (its own code, so the client can say
        "already on this version" instead of "not on an earlier version");
        else nothing.
    """

    if instance.schema_id != target_schema.id:
        return []
    return [
        ValidationFinding(
            rule="M0",
            message=(
                f"instance '{instance.id}' already runs on schema "
                f"'{target_schema.id}'; there is nothing to migrate"
            ),
            code="M0.already-on-target",
        )
    ]


#: Public name of the M0 check (see :func:`_check_m0`).
already_on_target = _check_m0


def _check_m1(
    target_schema: ProcessSchema, resolver: SchemaResolver | None
) -> list[ValidationFinding]:
    """M1: the target is RELEASED and correct, up to runtime-safe old findings.

    A migration changes nothing in the target; it only moves the case. So the
    question is not "does the move add a finding" but "may a case run on this
    target". A finding of a released target is always an old one (see
    :data:`RUNTIME_SAFE_OLD_FINDINGS`); one of those the runtime guards
    against is tolerated -- otherwise a case of the previous version could
    never move onto a revision that an old value made invalid -- every other
    one still refuses the move.

    :param target_schema: the schema the case should move onto.
    :param resolver: as for :func:`~procworks.validator.validate`.
    :returns: ``M1.not-released`` for a target that is not RELEASED, plus one
        ``M1.incorrect`` (``params.rule`` = the rule) per finding of the target
        that is not runtime-safe. A draft target reports every finding: the
        tolerance covers only what a release once accepted.
    """

    findings: list[ValidationFinding] = []
    released = target_schema.lifecycle_state is LifecycleState.RELEASED
    if target_schema.lifecycle_state is not LifecycleState.RELEASED:
        findings.append(
            ValidationFinding(
                rule="M1",
                message=(
                    f"target schema '{target_schema.id}' is not RELEASED "
                    f"(state {target_schema.lifecycle_state.value})"
                ),
                code="M1.not-released",
                params={"version": str(target_schema.version)},
            )
        )
    for f in validate(target_schema, resolver):
        if released and f.code in RUNTIME_SAFE_OLD_FINDINGS:
            continue
        findings.append(
            ValidationFinding(
                rule="M1",
                message=f"target schema is not correct: [{f.rule}] {f.message}",
                node_id=f.node_id,
                code="M1.incorrect",
                params={"rule": f.rule},
            )
        )
    return findings


def _check_m2(
    instance: ProcessInstance,
    source_schema: ProcessSchema,
    target_schema: ProcessSchema,
) -> list[ValidationFinding]:
    """Executed region (nodes + edges among them) preserved in the target."""

    findings: list[ValidationFinding] = []
    frozen = _frozen_nodes(instance)
    for nid in sorted(frozen):
        source_node = source_schema.nodes.get(nid)
        target_node = target_schema.nodes.get(nid)
        if target_node is None:
            findings.append(
                ValidationFinding(
                    rule="M2",
                    message=f"executed node '{nid}' is missing in the target schema",
                    node_id=nid,
                    code="M2.step-removed",
                    params={"step": node_name(source_schema, nid)},
                )
            )
        elif source_node is not None and target_node.type is not source_node.type:
            findings.append(
                ValidationFinding(
                    rule="M2",
                    message=(
                        f"executed node '{nid}' changed type "
                        f"({source_node.type.value} -> {target_node.type.value})"
                    ),
                    node_id=nid,
                    code="M2.step-changed",
                    params={"step": node_name(source_schema, nid)},
                )
            )
    # control edges with both endpoints frozen must be identical in both schemas
    source_internal = _internal_edges(source_schema, frozen)
    target_internal = _internal_edges(target_schema, frozen)
    for key in sorted(source_internal - target_internal):
        findings.append(
            ValidationFinding(
                rule="M2",
                message=f"executed control edge '{key}' is missing in the target",
                code="M2.path-changed",
                params=_edge_params(source_schema, key),
            )
        )
    for key in sorted(target_internal - source_internal):
        findings.append(
            ValidationFinding(
                rule="M2",
                message=f"target adds control edge '{key}' inside the executed region",
                code="M2.path-changed",
                params=_edge_params(target_schema, key),
            )
        )
    return findings


def _check_m3(
    instance: ProcessInstance,
    source_schema: ProcessSchema,
    target_schema: ProcessSchema,
) -> list[ValidationFinding]:
    """Markings map cleanly: completed nodes keep their successors, RUNNING
    nodes stay executable."""

    findings: list[ValidationFinding] = []
    for nid, state in instance.node_states.items():
        if state is NodeState.COMPLETED:
            source_out = {e.target for e in source_schema.outgoing(nid)}
            target_out = {e.target for e in target_schema.outgoing(nid)}
            if nid in target_schema.nodes and source_out != target_out:
                findings.append(
                    ValidationFinding(
                        rule="M3",
                        message=(
                            f"completed node '{nid}' would be rewired "
                            "(its successors changed in the target)"
                        ),
                        node_id=nid,
                        code="M3.rewired",
                        params={"step": node_name(source_schema, nid)},
                    )
                )
        elif state is NodeState.RUNNING:
            target_node = target_schema.nodes.get(nid)
            if target_node is None:
                findings.append(
                    ValidationFinding(
                        rule="M3",
                        message=f"running node '{nid}' is missing in the target",
                        node_id=nid,
                        code="M3.running-removed",
                        params={"step": node_name(source_schema, nid)},
                    )
                )
            elif target_node.type not in (NodeType.ACTIVITY, NodeType.SUBPROCESS):
                findings.append(
                    ValidationFinding(
                        rule="M3",
                        message=(
                            f"running node '{nid}' is no longer an executable step "
                            f"in the target (type {target_node.type.value})"
                        ),
                        node_id=nid,
                        code="M3.running-removed",
                        params={"step": node_name(source_schema, nid)},
                    )
                )
    return findings


def missing_mandatory_data(
    instance: ProcessInstance,
    target_schema: ProcessSchema,
    data_mapping: dict[str, object] | None = None,
) -> list[tuple[str, str]]:
    """Mandatory reads the target would leave without a value after migration.

    Two cases, both answered from the target's data accesses and the instance's
    marking (the D1 argument no longer holds for an instance that is already
    under way, because part of the path is behind it):

    1. **Executed reader.** A node that already ran (or is running) reads the
       element mandatorily -- the value was never produced under the source.
    2. **Future reader, past writer.** A node that has *not* run yet reads the
       element mandatorily, and one of its writers in the target is a node the
       instance has already COMPLETED. D1 holds for the target statically (the
       writer precedes the reader), but that writer will not run again, so the
       reader would read a value nobody writes. Conservative on purpose: a
       second writer ahead of the front could still supply it, but "maybe" is
       not enough for a mandatory input.

    A writer that is only SKIPPED is ignored (its branch was not taken; D1 then
    guarantees a writer on the taken path, which case 2 inspects on its own).
    Elements already present in the instance or in ``data_mapping`` never count.

    Returns ``(element_id, reader_node_id)`` pairs, one per affected reader,
    sorted for a stable report. The migration assistant uses the element ids to
    ask for start values; :func:`_check_m4` turns the pairs into findings.
    """

    available = set(instance.data_values) | set(data_mapping or {})
    frozen = _frozen_nodes(instance)
    completed = {
        nid for nid, state in instance.node_states.items() if state is NodeState.COMPLETED
    }
    past_writers: set[str] = {
        a.element_id
        for a in target_schema.data_accesses
        if a.mode in WRITE_MODES and a.node_id in completed
    }
    missing: set[tuple[str, str]] = set()
    for access in target_schema.data_accesses:
        if not access.mandatory or access.mode not in READ_MODES:
            continue
        if access.element_id in available:
            continue
        if access.node_id in frozen or access.element_id in past_writers:
            missing.add((access.element_id, access.node_id))
    return sorted(missing)


def _check_m4(
    instance: ProcessInstance,
    target_schema: ProcessSchema,
    data_mapping: dict[str, object] | None,
) -> list[ValidationFinding]:
    """Mandatory data must be available wherever the target can no longer
    produce it (see :func:`missing_mandatory_data`)."""

    findings: list[ValidationFinding] = []
    frozen = _frozen_nodes(instance)
    for element_id, node_id in missing_mandatory_data(instance, target_schema, data_mapping):
        element = target_schema.data_elements.get(element_id)
        name = element.name if element is not None else element_id
        where = "executed" if node_id in frozen else "upcoming"
        findings.append(
            ValidationFinding(
                rule="M4",
                message=(
                    f"mandatory data '{name}' read by {where} node '{node_id}' has "
                    f"no value in the instance and cannot be produced any more"
                ),
                node_id=node_id,
                code="M4.missing-data",
                params={"element": name, "step": node_name(target_schema, node_id)},
            )
        )
    return findings


# --- lineage (migration assistant) ---------------------------------------


def predecessor_ids(
    target: ProcessSchema,
    schemas: Iterable[ProcessSchema],
) -> set[str]:
    """Ids of all earlier revisions of ``target`` (its migration sources).

    Follows ``revision_of`` from the target upwards. Revisions created before
    that field existed carry ``None``; when the chain ends at such a schema with
    ``version > 1``, every schema with the **same name and a lower version** is
    taken as a predecessor too (the only lineage those carry). The target
    itself is never included, and a cycle in the chain (only possible through a
    hand-edited store) ends the walk instead of looping.

    Whether an instance of a predecessor may actually move is decided by M1-M5
    alone -- this function only narrows *which* instances are worth checking.
    """

    by_id = {s.id: s for s in schemas}
    result: set[str] = set()
    root = target
    seen = {target.id}
    while root.revision_of is not None and root.revision_of not in seen:
        parent = by_id.get(root.revision_of)
        if parent is None:
            break
        result.add(parent.id)
        seen.add(parent.id)
        root = parent
    if root.revision_of is None and root.version > 1:
        result |= {
            s.id
            for s in by_id.values()
            if s.name == root.name and s.version < root.version and s.id != target.id
        }
    return result


def superseded_by_release(schemas: Iterable[ProcessSchema]) -> set[str]:
    """Ids of the schemas that have a newer RELEASED revision.

    Walks the explicit lineage (``revision_of``) upwards from every RELEASED
    schema and marks each ancestor: an ancestor is superseded however many
    versions lie in between, also when a version in between was withdrawn.
    One pass over the store (each lineage walked once per released schema),
    so a listing over all schemas stays cheap.

    Used by the sub-process library: the newest released version of a family
    decides whether it is offered, so a revision without the library flag
    withdraws the offer. Unlike :func:`predecessor_ids` there is no fallback
    to "same name, lower version" for schemas without lineage -- a library
    offer must not disappear because an unrelated schema shares the name.

    :param schemas: the stored schemas.
    :returns: the ids of all schemas with at least one RELEASED descendant;
        a cycle in the lineage (only through a hand-edited store) ends the walk.
    """

    by_id = {s.id: s for s in schemas}
    superseded: set[str] = set()
    for schema in by_id.values():
        if schema.lifecycle_state is not LifecycleState.RELEASED:
            continue
        seen = {schema.id}
        parent_id = schema.revision_of
        while parent_id is not None and parent_id not in seen and parent_id in by_id:
            superseded.add(parent_id)
            seen.add(parent_id)
            parent_id = by_id[parent_id].revision_of
    return superseded


def latest_successor(
    source_id: str,
    schemas: Iterable[ProcessSchema],
) -> ProcessSchema | None:
    """The newest RELEASED revision that has ``source_id`` as a predecessor.

    Used by the run view to offer "migrate to the new version" on a single
    instance. ``None`` when no released successor exists.
    """

    pool = list(schemas)
    best: ProcessSchema | None = None
    for schema in pool:
        if schema.lifecycle_state is not LifecycleState.RELEASED or schema.id == source_id:
            continue
        if source_id not in predecessor_ids(schema, pool):
            continue
        if best is None or schema.version > best.version:
            best = schema
    return best


def _check_m5(instance: ProcessInstance) -> list[ValidationFinding]:
    """Ad-hoc instances require manual resolution (conservative)."""

    if instance.ad_hoc_deltas or instance.ad_hoc_schema is not None:
        return [
            ValidationFinding(
                rule="M5",
                message=(
                    "instance carries ad-hoc deltas; automatic migration is "
                    "blocked pending manual resolution"
                ),
                code="M5.adhoc",
            )
        ]
    return []
