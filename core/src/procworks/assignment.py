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

from collections.abc import Iterable
from datetime import UTC, datetime

from pydantic import BaseModel

from procworks import worklist_priority
from procworks.model import (
    PRIORITY_RANK,
    AbsenceEntry,
    InstanceState,
    NodeState,
    NodeType,
    OrgModel,
    PriorityLevel,
    ProcessInstance,
    ProcessSchema,
    StaffRule,
    StaffRuleKind,
    TimeCriticality,
    WorkItemPriority,
)
from procworks.worklist_priority import TimeContext

#: Default work-item priority when a node carries no explicit annotation.
_DEFAULT_PRIORITY = WorkItemPriority()


class OpenTask(BaseModel):
    """An open, human-assigned activity of a running instance.

    The time fields (``target_seconds`` .. ``time_criticality``) are the derived,
    never-persisted view of the time-based worklist prioritisation
    (Zeitbasierte-Priorisierung-Konzept). They are all optional / ``NONE`` by
    default, so a task without a modelled target time behaves exactly as before.
    """

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
    #: Resolved reaction target time in seconds (fallback rule S), or ``None``
    #: when the node carries no target time.
    target_seconds: float | None = None
    #: Absolute due time (activation + target), or ``None``.
    due_at: datetime | None = None
    #: Seconds elapsed since the node became ready, or ``None`` without a clock.
    elapsed_seconds: float | None = None
    #: Target minus elapsed (negative once overdue), or ``None``.
    remaining_seconds: float | None = None
    #: Derived time criticality band; ``NONE`` when the task has no target time
    #: or no activation clock (backward-compatible default).
    time_criticality: TimeCriticality = TimeCriticality.NONE


def absent_agent_ids(
    entries: Iterable[AbsenceEntry], now: datetime
) -> frozenset[str]:
    """Resolve the set of agents that are absent at wall-clock ``now``.

    An agent is absent while ``now`` lies within an entry's inclusive
    ``[start_at, end_at]`` window. Pure and IO-free (like the priority logic):
    the API boundary passes the store's entries and the current time, and hands
    the result to :func:`eligible_agents` / :func:`open_tasks`. Multiple entries
    for one agent are fine -- the union of their windows applies.

    Window bounds are compared timezone-aware; a naive bound (a client that
    omitted the offset) is treated as UTC so the comparison can never raise on
    mixed awareness -- stability over strictness.
    """

    return frozenset(
        entry.agent_id
        for entry in entries
        if _as_utc(entry.start_at) <= now <= _as_utc(entry.end_at)
    )


def _as_utc(moment: datetime) -> datetime:
    """Return ``moment`` as timezone-aware UTC (a naive value is assumed UTC)."""

    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def eligible_agents(
    schema: ProcessSchema,
    node_id: str,
    instance: ProcessInstance,
    *,
    include_deputies: bool = True,
    absent_agents: frozenset[str] = frozenset(),
) -> set[str]:
    """Return the concrete agent ids currently eligible to perform ``node_id``.

    Returns an empty set when the node carries no staff rule (an automatic or
    unassigned step). NodePerformingAgent terms resolve against the agents that
    actually performed the referenced nodes in this instance.

    **Deputy (Vertreter) substitution is absence-gated:** a deputy is added to
    the eligible set only for a base agent that is currently absent (its id is in
    ``absent_agents``), so during an agent's holiday/out-of-office window the
    deputy receives the task *in parallel*. Outside any absence -- the default,
    empty ``absent_agents`` -- no deputy is added and only the rule's own agents
    are eligible. The base agents are **never removed** by this step, so an
    absent agent without a registered deputy keeps the task rather than the
    instance stalling (safety invariant). ``include_deputies=False`` disables
    substitution entirely (used by a mail notification that opts out).
    """

    rule = schema.staff_rules.get(node_id)
    if rule is None:
        return set()
    base = _resolve(schema.org_model, rule, instance)
    if not include_deputies:
        return base
    return _with_deputies(schema.org_model, base, absent_agents)


def open_tasks(
    schema: ProcessSchema,
    instance: ProcessInstance,
    ctx: TimeContext | None = None,
    *,
    absent_agents: frozenset[str] = frozenset(),
) -> list[OpenTask]:
    """List the open human tasks of a running instance with their eligibles.

    An open human task is an ACTIVATED/RUNNING ACTIVITY that carries a staff
    rule. Automatic or unassigned activities are not part of a worklist.

    Ordering:

    * **without** a ``ctx`` (the default): as before -- by derived business
      priority (most urgent first, roadmap E8), then label/node_id. Fully
      backward compatible.
    * **with** a ``TimeContext``: by the time-based lexicographic key of the
      prioritisation concept (Section 5.3) -- time criticality band first
      (deadline risk dominates), then business priority, then earliest due date,
      then label/node_id. The time fields of each :class:`OpenTask` are filled
      from the derived assessment.

    The context is the only behaviour-changing input; passing ``None`` keeps the
    historical behaviour exactly, so untimed models are unaffected (leitplanke
    L3).
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
        task = OpenTask(
            instance_id=instance.id,
            schema_id=instance.schema_id,
            schema_version=instance.schema_version,
            node_id=node_id,
            label=node.label or node_id,
            eligible_agents=sorted(
                eligible_agents(schema, node_id, instance, absent_agents=absent_agents)
            ),
            priority=priority.level,
        )
        if ctx is not None:
            view = worklist_priority.assess(schema, node_id, ctx)
            task.target_seconds = view.target_seconds
            task.due_at = view.due_at
            task.elapsed_seconds = view.elapsed_seconds
            task.remaining_seconds = view.remaining_seconds
            task.time_criticality = view.criticality
        tasks.append(task)
    if ctx is None:
        tasks.sort(key=lambda t: (-PRIORITY_RANK[t.priority], t.label, t.node_id))
    else:
        tasks.sort(
            key=lambda t: worklist_priority.sort_key(
                t.time_criticality,
                PRIORITY_RANK[t.priority],
                t.due_at,
                t.label,
                t.node_id,
            )
        )
    return tasks


def _resolve(org: OrgModel, rule: StaffRule, instance: ProcessInstance) -> set[str]:
    """Resolve a staff rule to a concrete set of agent ids for this instance."""

    if rule.kind is StaffRuleKind.ROLE:
        return {a.id for a in org.agents.values() if rule.ref in a.role_ids}
    if rule.kind is StaffRuleKind.ORG_UNIT:
        units = {rule.ref} | _descendant_units(org, rule.ref, rule.recursive)
        return {a.id for a in org.agents.values() if a.org_unit_id in units}
    if rule.kind is StaffRuleKind.AGENT:
        return {rule.ref} if rule.ref in org.agents else set()
    if rule.kind is StaffRuleKind.NODE_PERFORMING_AGENT:
        performer = instance.performed_by.get(rule.ref) if rule.ref else None
        return {performer} if performer is not None else set()
    if rule.kind is StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR:
        supervisor = _supervisor_of_performer(org, rule, instance)
        return {supervisor} if supervisor is not None else set()
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


def _supervisor_of_performer(
    org: OrgModel, rule: StaffRule, instance: ProcessInstance
) -> str | None:
    """Resolve the supervisor of the agent who performed ``rule.ref``.

    The supervisor is the manager of the performer's org unit
    (``OrgUnit.manager_id``). Returns ``None`` -- an empty eligible set -- when
    the referenced node has no recorded performer yet, the performer is not in
    the org model, has no org unit, or the org unit has no manager. This mirrors
    the design-time over-approximation used by Z2 (all org-unit managers).
    """

    performer_id = instance.performed_by.get(rule.ref) if rule.ref else None
    if performer_id is None:
        return None
    performer = org.agents.get(performer_id)
    if performer is None or performer.org_unit_id is None:
        return None
    unit = org.org_units.get(performer.org_unit_id)
    if unit is None:
        return None
    return unit.manager_id


def _with_deputies(
    org: OrgModel, base: set[str], absent: frozenset[str]
) -> set[str]:
    """Extend an eligible set by the deputies of *absent* agents.

    Follows the substitution chain transitively but only steps across a deputy
    edge when the agent on the near side is currently absent: a present agent
    keeps their own tasks, an absent one hands them to their deputy in parallel.
    If the deputy is in turn absent, their deputy is added too (chain), with a
    visited guard so deputy cycles terminate. The base agents are always kept.
    """

    result = set(base)
    frontier = list(base)
    while frontier:
        agent = org.agents.get(frontier.pop())
        if agent is None or agent.deputy_id is None:
            continue
        if agent.id not in absent:
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
