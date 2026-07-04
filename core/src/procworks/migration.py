# SPDX-License-Identifier: BUSL-1.1
"""Schema evolution and instance migration (roadmap step 11, M1-M5).

When a new schema revision is released, each running instance is checked
*individually* against the migration criteria before it may switch versions:

  * M1  target is itself a correct, RELEASED (executable) schema;
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

from procworks.model import (
    READ_MODES,
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
    validate,
)

#: Node states that count as "already progressed" and therefore frozen.
_FROZEN = frozenset({NodeState.COMPLETED, NodeState.RUNNING, NodeState.SKIPPED})


def _edge_key(source: str, target: str) -> str:
    return f"{source}->{target}"


def _frozen_nodes(instance: ProcessInstance) -> set[str]:
    return {nid for nid, st in instance.node_states.items() if st in _FROZEN}


def check_migration(
    instance: ProcessInstance,
    source_schema: ProcessSchema,
    target_schema: ProcessSchema,
    *,
    resolver: SchemaResolver | None = None,
    data_mapping: dict[str, object] | None = None,
) -> list[ValidationFinding]:
    """Return the M1-M5 findings for migrating ``instance`` onto the target.

    An empty list means the instance is migratable.
    """

    findings: list[ValidationFinding] = []
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

    requires: M1-M5 hold (else CorrectnessError with the findings).
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
    new_node_states = {nid: NodeState.NOT_ACTIVATED for nid in target_schema.nodes}
    for nid in target_schema.nodes:
        if nid in instance.node_states:
            new_node_states[nid] = instance.node_states[nid]
    new_edge_states: dict[str, EdgeState] = {}
    for edge in target_schema.edges:
        key = _edge_key(edge.source, edge.target)
        new_edge_states[key] = instance.edge_states.get(key, EdgeState.NOT_SIGNALED)
    result.node_states = new_node_states
    result.edge_states = new_edge_states
    if data_mapping:
        result.data_values.update(data_mapping)
    return result


# --- criteria ------------------------------------------------------------


def _check_m1(
    target_schema: ProcessSchema, resolver: SchemaResolver | None
) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    if target_schema.lifecycle_state is not LifecycleState.RELEASED:
        findings.append(
            ValidationFinding(
                rule="M1",
                message=(
                    f"target schema '{target_schema.id}' is not RELEASED "
                    f"(state {target_schema.lifecycle_state.value})"
                ),
            )
        )
    for f in validate(target_schema, resolver):
        findings.append(
            ValidationFinding(
                rule="M1",
                message=f"target schema is not correct: [{f.rule}] {f.message}",
                node_id=f.node_id,
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
                )
            )
    # control edges with both endpoints frozen must be identical in both schemas
    source_internal = {
        _edge_key(e.source, e.target)
        for e in source_schema.edges
        if e.source in frozen and e.target in frozen
    }
    target_internal = {
        _edge_key(e.source, e.target)
        for e in target_schema.edges
        if e.source in frozen and e.target in frozen
    }
    for key in sorted(source_internal - target_internal):
        findings.append(
            ValidationFinding(
                rule="M2",
                message=f"executed control edge '{key}' is missing in the target",
            )
        )
    for key in sorted(target_internal - source_internal):
        findings.append(
            ValidationFinding(
                rule="M2",
                message=f"target adds control edge '{key}' inside the executed region",
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
                    )
                )
    return findings


def _check_m4(
    instance: ProcessInstance,
    target_schema: ProcessSchema,
    data_mapping: dict[str, object] | None,
) -> list[ValidationFinding]:
    """Mandatory data for the executed region must be available."""

    findings: list[ValidationFinding] = []
    available = set(instance.data_values) | set(data_mapping or {})
    frozen = _frozen_nodes(instance)
    for access in target_schema.data_accesses:
        if not access.mandatory:
            continue
        if access.mode not in READ_MODES:
            continue
        if access.node_id not in frozen:
            continue  # required only ahead of the front -> fine
        if access.element_id not in available:
            element = target_schema.data_elements.get(access.element_id)
            name = element.name if element is not None else access.element_id
            findings.append(
                ValidationFinding(
                    rule="M4",
                    message=(
                        f"mandatory data '{name}' read by executed node "
                        f"'{access.node_id}' has no value in the instance"
                    ),
                    node_id=access.node_id,
                )
            )
    return findings


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
            )
        ]
    return []
