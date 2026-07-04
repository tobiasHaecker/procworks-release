# SPDX-License-Identifier: BUSL-1.1
"""Runtime staff resolution and the per-agent task list (roadmap step 13a).

The structural validator (Z1-Z3) reasons about a staff rule (BZR) at design
time: an *over-approximation* of who could perform a node, used to prove the
rule is satisfiable. This module is the runtime counterpart: given a concrete
running instance it resolves a node's staff rule to the *concrete* set of
agents currently eligible to work it, and lists the open human tasks.

Two organisational features feed the resolution:

* a task addressed to a role or an org unit shows up for *every* member, and
  any one of them may take it (the eligible set is a union over members);
* an org unit rule may be ``recursive`` to include all sub-units (the ADEPT
  ``*`` modifier), i.e. the unit itself or the unit and everything below it;
* each agent may name a deputy (Vertreter); whenever an agent is eligible the
  deputy is eligible too, following the substitution chain transitively (with
  a visited guard so deputy cycles terminate).

The module holds no correctness logic of its own -- it only *reads* the model
and the instance markings.
"""

from __future__ import annotations

from pydantic import BaseModel

from procworks.model import (
    PRIORITY_RANK,
    InstanceState,
    NodeState,
    NodeType,
    OrgModel,
    PriorityLevel,
    ProcessInstance,
    ProcessSchema,
    StaffRule,
    StaffRuleKind,
    WorkItemPriority,
)

#: Default work-item priority when a node carries no explicit annotation.
_DEFAULT_PRIORITY = WorkItemPriority()


class OpenTask(BaseModel):
    """An open, human-assigned activity of a running instance."""

    instance_id: str
    schema_id: str
    #: Version (revision) of the schema this instance runs against. Carried so
    #: worklists can show which revision a task belongs to (revisions share the
    #: same ``name`` but get a fresh ``schema_id`` and an incremented version).
    schema_version: int = 1
    node_id: str
    label: str
    eligible_agents: list[str]
    #: Derived work-item priority level (roadmap E8). ``MEDIUM`` by default.
    priority: PriorityLevel = PriorityLevel.MEDIUM


def eligible_agents(
    schema: ProcessSchema, node_id: str, instance: ProcessInstance
) -> set[str]:
    """Return the concrete agent ids currently eligible to perform ``node_id``.

    Returns an empty set when the node carries no staff rule (an automatic or
    unassigned step). NodePerformingAgent terms resolve against the agents that
    actually performed the referenced nodes in this instance; deputies are
    added on top.
    """

    rule = schema.staff_rules.get(node_id)
    if rule is None:
        return set()
    base = _resolve(schema.org_model, rule, instance)
    return _with_deputies(schema.org_model, base)


def open_tasks(schema: ProcessSchema, instance: ProcessInstance) -> list[OpenTask]:
    """List the open human tasks of a running instance with their eligibles.

    An open human task is an ACTIVATED/RUNNING ACTIVITY that carries a staff
    rule. Automatic or unassigned activities are not part of a worklist. The
    list is ordered by derived priority (most urgent first, roadmap E8) and
    then by label for a stable, predictable ordering.
    """

    tasks: list[OpenTask] = []
    if instance.state is not InstanceState.RUNNING:
        return tasks
    for node_id, node_state in instance.node_states.items():
        if node_state not in (NodeState.ACTIVATED, NodeState.RUNNING):
            continue
        node = schema.nodes.get(node_id)
        if node is None or node.type is not NodeType.ACTIVITY:
            continue
        if node_id not in schema.staff_rules:
            continue
        priority = schema.node_priorities.get(node_id, _DEFAULT_PRIORITY)
        tasks.append(
            OpenTask(
                instance_id=instance.id,
                schema_id=instance.schema_id,
                schema_version=instance.schema_version,
                node_id=node_id,
                label=node.label or node_id,
                eligible_agents=sorted(eligible_agents(schema, node_id, instance)),
                priority=priority.level,
            )
        )
    tasks.sort(key=lambda t: (-PRIORITY_RANK[t.priority], t.label, t.node_id))
    return tasks


def _resolve(org: OrgModel, rule: StaffRule, instance: ProcessInstance) -> set[str]:
    """Resolve a staff rule to a concrete set of agent ids for this instance."""

    if rule.kind is StaffRuleKind.ROLE:
        return {a.id for a in org.agents.values() if rule.ref in a.role_ids}
    if rule.kind is StaffRuleKind.ORG_UNIT:
        units = {rule.ref} | _descendant_units(org, rule.ref, rule.recursive)
        return {a.id for a in org.agents.values() if a.org_unit_id in units}
    if rule.kind is StaffRuleKind.NODE_PERFORMING_AGENT:
        performer = instance.performed_by.get(rule.ref) if rule.ref else None
        return {performer} if performer is not None else set()
    operands = [_resolve(org, op, instance) for op in rule.operands]
    if not operands:
        return set()
    if rule.kind is StaffRuleKind.AND:
        result = set(operands[0])
        for operand in operands[1:]:
            result &= operand
        return result
    if rule.kind is StaffRuleKind.OR:
        result = set()
        for operand in operands:
            result |= operand
        return result
    # EXCEPT: left minus right.
    return operands[0] - operands[1] if len(operands) >= 2 else set(operands[0])


def _with_deputies(org: OrgModel, base: set[str]) -> set[str]:
    """Extend an eligible set by deputies, following chains transitively."""

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
