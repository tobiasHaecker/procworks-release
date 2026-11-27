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

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from procworks import worklist_priority
from procworks.model import (
    PRIORITY_RANK,
    AbsenceEntry,
    EscalationKind,
    InstanceState,
    Node,
    NodeDetailState,
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


class DisplayValue(BaseModel):
    """One value that names an instance (``ProcessSchema.display_fields``).

    ``value`` is the raw instance value (``None`` while unset); wording and
    number/date formatting are the client's matter, as everywhere.
    """

    element_id: str
    name: str
    value: object | None = None
    #: Data type of the element (``DataType`` value), so the client formats
    #: a customer number differently from an amount. ``None`` for old data.
    data_type: str | None = None


def display_values(schema: ProcessSchema, instance: ProcessInstance) -> list[DisplayValue]:
    """The naming values of an instance, in the order the model lists them.

    Pure read. Elements that no longer exist are skipped (U5 prevents that on
    the current schema; an instance may still run on an older revision).
    """

    result: list[DisplayValue] = []
    for element_id in schema.display_fields:
        element = schema.data_elements.get(element_id)
        if element is None:
            continue
        result.append(
            DisplayValue(
                element_id=element_id,
                name=element.name,
                value=instance.data_values.get(element_id),
                data_type=element.data_type.value,
            )
        )
    return result


class OpenTask(BaseModel):
    """An open, human-assigned activity of a running instance.

    The time fields (``target_seconds`` .. ``time_criticality``) are the derived,
    never-persisted view of the time-based worklist prioritisation
    (see :mod:`procworks.worklist_priority`). They are all optional / ``NONE`` by
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
    #: Owner of the work item (E1): the agent who claimed the step, or ``None``
    #: while the task is an open offer. Purely informative here -- the
    #: per-agent Withdrawn filtering happens at the API boundary, so this
    #: instance-wide view stays complete (transparency in monitoring).
    claimed_by: str | None = None
    #: Wall-clock instant of the claim (boundary-stamped), or ``None``.
    claimed_at: datetime | None = None
    #: Fired escalation stages of the current activation (T3/E9); ``0`` while
    #: nothing escalated. Lets worklists mark an escalated task.
    escalated_stage: int = 0
    #: Runtime detail overlay (E2): SUSPENDED/FAILED, or ``None``. Lets the
    #: owner's list offer resume/reset and monitoring show the pause/failure.
    detail: NodeDetailState | None = None
    #: Reason text of a FAILED detail (empty otherwise).
    detail_reason: str = ""
    #: The values that name the instance (``display_fields``) -- two
    #: equal tasks of different instances were indistinguishable in a worklist.
    context: list[DisplayValue] = Field(default_factory=list)
    #: When the instance started (``ProcessInstance.started_at``), or ``None``
    #: for instances from before the stamp. Lets a worklist tell two tasks of
    #: the same step apart while the naming values above are still empty --
    #: typically at the very first step, which is what sets them.
    instance_started_at: datetime | None = None
    #: Deputies among ``eligible_agents``: deputy agent id -> the absent agent
    #: they stand in for (the one whose deputy edge added them; along a chain
    #: the absent deputy before them). Lets a worklist say "in Vertretung für
    #: …" -- a deputy saw a colleague's task without knowing why. Agents who
    #: are eligible on their own never appear here.
    deputy_of: dict[str, str] = Field(default_factory=dict)


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
    deputy_of: dict[str, str] | None = None,
    excluded_deputies: dict[str, set[str]] | None = None,
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

    ``deputy_of``, when given, is filled with every deputy this call added and
    the absent agent it stands in for (see :attr:`OpenTask.deputy_of`); the
    returned set is the same with or without it.

    ``excluded_deputies``, when given, is filled with every deputy edge the
    four-eyes principle blocked: deputy id -> the absent agents it would have
    stood in for (see :func:`unstaffed_steps`). Same returned set either way.
    """

    rule = schema.staff_rules.get(node_id)
    if rule is None:
        return set()
    base = _resolve(schema.org_model, rule, instance)
    # The separation of duties the rule demands (four eyes): nobody who did a
    # step this rule excludes, or whose work it has a supervisor approve, may
    # be added back -- neither by an escalation stage nor by a deputy edge.
    never = _four_eyes_performers(rule, instance, schema.org_model)
    # T3/E9: fired FUNCTIONAL escalation stages *broaden* the offer -- their
    # target sets join the eligibles (never replacing the original set, so the
    # no-empty-set safety invariant holds trivially). HIERARCHICAL stages only
    # inform and never appear here.
    policy = schema.escalation_policies.get(node_id)
    if policy is not None:
        fired = instance.escalated_stages.get(node_id, 0)
        for stage in policy.stages[:fired]:
            if stage.kind is EscalationKind.FUNCTIONAL:
                base = base | (_resolve(schema.org_model, stage.rule, instance) - never)
    if not include_deputies:
        return base
    return _with_deputies(
        schema.org_model,
        base,
        absent_agents,
        never,
        via=deputy_of,
        blocked=excluded_deputies,
    )


def four_eyes_reference(
    schema: ProcessSchema,
    node_id: str,
    instance: ProcessInstance,
    agent_id: str,
) -> str | None:
    """Name the step whose performer ``agent_id`` is, if that bars them from ``node_id``.

    Explains a refusal: an agent who holds the right role but did an earlier
    step that the rule of ``node_id`` excludes ("anyone of the role, except
    whoever did step X") or has a supervisor approve is kept out by the
    four-eyes principle -- a plain "not responsible" would leave them puzzled.

    :param schema: the effective schema of the instance.
    :param node_id: the step the agent wants to claim or complete.
    :param instance: the case (its ``performed_by`` decides).
    :param agent_id: the acting agent.
    :returns: the id of the referenced earlier step the agent performed, or
        ``None`` when the step has no rule or the four-eyes part of the rule
        does not concern this agent. With several such steps, the smallest
        step id is returned (deterministic).
    """

    rule = schema.staff_rules.get(node_id)
    if rule is None:
        return None
    steps = sorted(
        step
        for step, performer in _four_eyes_steps(rule, instance, schema.org_model).items()
        if performer == agent_id
    )
    return steps[0] if steps else None


def resolve_rule_agents(
    schema: ProcessSchema,
    rule: StaffRule,
    instance: ProcessInstance,
    *,
    absent_agents: frozenset[str] = frozenset(),
) -> set[str]:
    """Resolve a standalone staff rule to concrete agents (incl. deputies).

    The public counterpart of the per-node :func:`eligible_agents` for rules
    that are not bound to a node -- today the escalation stage targets (T3/E9),
    which the boundary resolves to notification recipients / added performers.
    Same absence-gated deputy semantics as everywhere.
    """

    base = _resolve(schema.org_model, rule, instance)
    return _with_deputies(
        schema.org_model,
        base,
        absent_agents,
        _four_eyes_performers(rule, instance, schema.org_model),
    )


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
      worklist prioritisation -- time criticality band first
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
    context = display_values(schema, instance)
    for node_id, node in _open_activities(schema, instance):
        if node_id not in schema.staff_rules:
            continue
        priority = schema.node_priorities.get(node_id, _DEFAULT_PRIORITY)
        deputy_of: dict[str, str] = {}
        task = OpenTask(
            instance_id=instance.id,
            schema_id=instance.schema_id,
            schema_version=instance.schema_version,
            node_id=node_id,
            label=node.label or node_id,
            eligible_agents=sorted(
                eligible_agents(
                    schema,
                    node_id,
                    instance,
                    absent_agents=absent_agents,
                    deputy_of=deputy_of,
                )
            ),
            priority=priority.level,
            claimed_by=instance.claimed_by.get(node_id),
            claimed_at=instance.node_claimed_at.get(node_id),
            escalated_stage=instance.escalated_stages.get(node_id, 0),
            detail=instance.node_details.get(node_id),
            detail_reason=instance.node_detail_reason.get(node_id, ""),
            context=context,
            instance_started_at=instance.started_at,
            deputy_of=deputy_of,
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


def _open_activities(
    schema: ProcessSchema, instance: ProcessInstance
) -> Iterator[tuple[str, Node]]:
    """Yield the open ACTIVITY nodes of ``instance`` as ``(node_id, node)``.

    Open means marked ACTIVATED or RUNNING. Nodes of the marking that the
    schema does not know are skipped, as are all non-ACTIVITY nodes (gateways,
    sub-processes). The order is that of ``instance.node_states``; callers sort
    their result themselves. Staff rules, service bindings and the instance
    state are deliberately *not* looked at here -- :func:`open_tasks` and
    :func:`unstaffed_steps` filter those differently.
    """

    for node_id, node_state in instance.node_states.items():
        if node_state not in (NodeState.ACTIVATED, NodeState.RUNNING):
            continue
        node = schema.nodes.get(node_id)
        if node is not None and node.type is NodeType.ACTIVITY:
            yield node_id, node


class ExcludedDeputy(BaseModel):
    """A registered deputy the four-eyes principle keeps from an unstaffed step.

    The deputy did a step that the rule of the reported step excludes (or has
    a supervisor approve), so standing in for the absent agent would mean
    checking their own work.

    :ivar agent_id: the deputy.
    :ivar stands_in_for: the absent agent whose deputy they are.
    :ivar step_id: the earlier step the deputy performed.
    :ivar step_label: its label (the monitoring spans processes and resolves
        nothing in a selected schema).
    """

    agent_id: str
    stands_in_for: str
    step_id: str
    step_label: str


class UnstaffedStep(BaseModel):
    """An open human step of a running instance that nobody may work.

    ``reason`` is one of

    * ``"no_rule"`` -- an interactive step without a staff rule (e.g. an
      ad-hoc step from before the B2 gate);
    * ``"nobody"`` -- the rule resolves to nobody right now (an empty EXCEPT,
      an org change, a supervision act before a four-eyes step);
    * ``"no_login"`` -- the rule finds people, but none of them can sign in
      (only reported when the caller knows the logins, see
      :func:`unstaffed_steps`);
    * ``"only_absent"`` -- every eligible person who could sign in is absent
      and no deputy is present. Weaker than the others: the task stays with
      them (the safety invariant of :func:`eligible_agents`) and moves on once
      someone is back, but meanwhile nobody works it.

    The first three stand still for good; before this report such an instance
    showed as "overdue 0, escalated 0". ``agent_ids`` names the eligible
    people behind ``no_login`` / ``only_absent`` (empty otherwise), so a
    supervisor sees *whom* to chase.

    ``excluded_deputies`` (only with ``only_absent``) names the deputies who
    *are* registered but may not stand in by the four-eyes principle -- they
    did the step this one checks. Without it the report read "absent without
    a deputy", and a supervisor would register yet another deputy or doubt
    the deputy rule. Empty when no deputy edge was blocked.
    """

    instance_id: str
    schema_id: str
    schema_version: int = 1
    node_id: str
    label: str
    reason: str
    agent_ids: list[str] = Field(default_factory=list)
    excluded_deputies: list[ExcludedDeputy] = Field(default_factory=list)


def unstaffed_steps(
    schema: ProcessSchema,
    instance: ProcessInstance,
    *,
    absent_agents: frozenset[str] = frozenset(),
    agents_with_login: frozenset[str] | None = None,
) -> list[UnstaffedStep]:
    """List the open human steps of ``instance`` that no agent can work.

    Pure read, never persisted. Considered are ACTIVATED/RUNNING ACTIVITY
    nodes of a RUNNING instance; automatic steps (service binding with
    ``automatic``) are machines' work and never listed. A step with a rule
    counts as unstaffed when :func:`eligible_agents` -- deputies of absent
    agents and fired FUNCTIONAL escalation stages included, exactly as the
    worklist resolves them -- is empty.

    This is the **last line of defence**: Z2 and the ad-hoc B2 gate prevent
    most such steps at modelling time, but an organisation changes during
    operation, and some rules (performer references) are only decided at
    runtime.

    Beyond an empty set, a step also counts as unstaffed when nobody in the
    eligible set can actually act: ``"no_login"`` when none of them has a
    login, ``"only_absent"`` when all who have one are absent (their deputies,
    if any, are already in the set -- a present deputy staffs the step).
    Assignment itself is untouched; this is a report.

    :param absent_agents: agents absent right now (deputy substitution and
        the ``only_absent`` check).
    :param agents_with_login: agent ids that can sign in. ``None`` means
        "unknown" (token/JWT modes accept people without a stored login) --
        then logins are not checked and only ``only_absent`` can add to the
        classic reasons.
    :returns: the unstaffed steps, sorted by label.
    """

    result: list[UnstaffedStep] = []
    if instance.state is not InstanceState.RUNNING:
        return result
    for node_id, node in _open_activities(schema, instance):
        binding = schema.service_bindings.get(node_id)
        if binding is not None and binding.automatic:
            continue
        agent_ids: list[str] = []
        blocked: dict[str, set[str]] = {}
        excluded: list[ExcludedDeputy] = []
        if node_id not in schema.staff_rules:
            reason = "no_rule"
        else:
            eligible = eligible_agents(
                schema, node_id, instance, absent_agents=absent_agents, excluded_deputies=blocked
            )
            if not eligible:
                reason = "nobody"
            else:
                can_sign_in = (
                    eligible if agents_with_login is None else eligible & agents_with_login
                )
                if not can_sign_in:
                    reason, agent_ids = "no_login", sorted(eligible)
                elif can_sign_in <= absent_agents:
                    reason, agent_ids = "only_absent", sorted(can_sign_in)
                    excluded = _excluded_deputies(
                        schema, schema.staff_rules[node_id], instance, blocked
                    )
                else:
                    continue
        result.append(
            UnstaffedStep(
                instance_id=instance.id,
                schema_id=instance.schema_id,
                schema_version=instance.schema_version,
                node_id=node_id,
                label=node.label or node_id,
                reason=reason,
                agent_ids=agent_ids,
                excluded_deputies=excluded,
            )
        )
    result.sort(key=lambda u: (u.label, u.node_id))
    return result


def _excluded_deputies(
    schema: ProcessSchema,
    rule: StaffRule,
    instance: ProcessInstance,
    blocked: dict[str, set[str]],
) -> list[ExcludedDeputy]:
    """Turn the blocked deputy edges of one step into report entries.

    :param schema: the effective schema (for the labels of the earlier steps).
    :param rule: the staff rule of the reported step.
    :param instance: the case (``performed_by`` names the earlier steps).
    :param blocked: deputy id -> absent agents, as filled by
        :func:`eligible_agents` (``excluded_deputies``).
    :returns: one entry per (deputy, absent agent), sorted by deputy and
        absent agent; the earlier step is the smallest step id the deputy
        performed among those the rule refers to (deterministic).
    """

    steps_by_performer: dict[str, str] = {}
    for step, performer in sorted(_four_eyes_steps(rule, instance, schema.org_model).items()):
        steps_by_performer.setdefault(performer, step)
    entries: list[ExcludedDeputy] = []
    for deputy in sorted(blocked):
        found = steps_by_performer.get(deputy)
        if found is None:
            continue
        node = schema.nodes.get(found)
        label = (node.label if node is not None else None) or found
        for absent in sorted(blocked[deputy]):
            entries.append(
                ExcludedDeputy(
                    agent_id=deputy, stands_in_for=absent, step_id=found, step_label=label
                )
            )
    return entries


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
        return operands[0].intersection(*operands[1:])
    if rule.kind is StaffRuleKind.OR:
        return set[str]().union(*operands)
    # EXCEPT: left minus right.
    return operands[0] - operands[1] if len(operands) >= 2 else set(operands[0])


def _supervisor_of_performer(
    org: OrgModel, rule: StaffRule, instance: ProcessInstance
) -> str | None:
    """Resolve the supervisor of the agent who performed ``rule.ref``.

    The supervisor is the nearest manager **above the performer**: the manager
    of the performer's org unit -- unless that is the performer themself or the
    unit has no manager; then the search goes up the ``parent_id`` chain
    -- so a unit head who files their own leave request is never its only
    approver: "Vorgesetzte:r" must not resolve to the performer themself, or
    the four-eyes principle would be gone.

    Returns ``None`` -- an empty eligible set, which the monitoring shows as
    "Niemand zuständig" -- when the referenced node has no recorded
    performer yet, the performer is not in the org model or has no org unit,
    or no unit up the chain has a manager other than the performer. The result
    is always the manager of *some* unit, so the design-time over-approximation
    of Z2 (all org-unit managers) still holds. A cycle in the parent chain
    (rejected by the org validation anyway) ends the search.
    """

    performer_id = instance.performed_by.get(rule.ref) if rule.ref else None
    if performer_id is None:
        return None
    performer = org.agents.get(performer_id)
    if performer is None or performer.org_unit_id is None:
        return None
    seen: set[str] = set()
    unit_id: str | None = performer.org_unit_id
    while unit_id is not None and unit_id not in seen:
        seen.add(unit_id)
        unit = org.org_units.get(unit_id)
        if unit is None:
            return None
        if unit.manager_id is not None and unit.manager_id != performer_id:
            return unit.manager_id
        unit_id = unit.parent_id
    return None


def _four_eyes_performers(
    rule: StaffRule, instance: ProcessInstance, org: OrgModel
) -> frozenset[str]:
    """Performers whose work a rule has someone *else* do or approve.

    The agents of :func:`_four_eyes_steps` -- see there which terms count.
    :func:`_with_deputies` never adds them as deputies, and
    :func:`eligible_agents` never lets an escalation stage add them back.
    """

    return frozenset(_four_eyes_steps(rule, instance, org).values())


def _four_eyes_steps(
    rule: StaffRule, instance: ProcessInstance, org: OrgModel
) -> dict[str, str]:
    """The earlier steps a rule keeps away from their performer, with that performer.

    Two kinds of term in the rule tree (at any depth of AND/OR/EXCEPT) express
    the four-eyes principle:

    * ``NODE_PERFORMING_AGENT_SUPERVISOR`` -- the supervisor of whoever did
      step X approves. The supervisor itself never is that performer (see
      :func:`_supervisor_of_performer`), but a deputy could be: were the
      supervisor absent and the performer their deputy, the substitution
      would hand the approval straight back.
    * ``NODE_PERFORMING_AGENT`` on an **excluding** side -- the right-hand
      operand of an ``EXCEPT`` ("anyone of the role, except whoever did step
      X"). The rule itself removes that performer, but a deputy edge or an
      escalation stage would add them again -- the person who did step X as
      the deputy of an absent colleague would be offered the check of their
      own work. An exclusion inside an exclusion selects again and does not
      count (it is the rule's own choice to admit that performer).

    **Exactly as the rule says:** such a performer counts only if the
    excluding side of the ``EXCEPT`` above the term, *evaluated* for this
    case, contains them. ``sb except (performer of X and sb)`` does not
    exclude a performer outside ``sb`` -- before, the mere term kept them
    out as a deputy too, stricter than the rule. Nested exclusions are
    evaluated as a whole (``sb except (performer of X except tl)`` admits a
    performer in ``tl``).

    The performer is the agent recorded in ``instance.performed_by`` -- the
    one who actually acted, also as a deputy or a bound supervisor. Steps
    without a recorded performer are skipped.

    :param rule: the staff rule of the step about to be offered.
    :param instance: the case.
    :param org: the organisation the rule resolves against (for the
        excluding sides).
    :returns: referenced step id -> its performer.
    """

    found: dict[str, str] = {}
    # (rule, excluding, set the nearest selecting EXCEPT subtracts or None)
    stack: list[tuple[StaffRule, bool, set[str] | None]] = [(rule, False, None)]
    while stack:
        current, excluding, subtracted = stack.pop()
        if current.ref:
            performer = instance.performed_by.get(current.ref)
            if performer is not None and (
                current.kind is StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR
                or (
                    current.kind is StaffRuleKind.NODE_PERFORMING_AGENT
                    and excluding
                    and subtracted is not None
                    and performer in subtracted
                )
            ):
                found[current.ref] = performer
        for index, operand in enumerate(current.operands):
            if current.kind is StaffRuleKind.EXCEPT and index >= 1:
                if excluding:
                    # An exception inside an exclusion selects again.
                    stack.append((operand, False, None))
                else:
                    stack.append((operand, True, _resolve(org, operand, instance)))
            else:
                stack.append((operand, excluding, subtracted))
    return found


def four_eyes_references(rule: StaffRule) -> frozenset[str]:
    """The steps a rule's four-eyes terms refer to -- independent of any case.

    The same terms as :func:`_four_eyes_steps` (a supervisor-of-performer
    term, or a performer term on an excluding side of ``EXCEPT``), without
    looking up who performed them. The ad-hoc path uses it to refuse
    removing a step that carries or anchors the four-eyes principle.

    :param rule: a staff rule.
    :returns: the referenced step ids (empty: the rule has no four-eyes part).
    """

    return frozenset(_four_eyes_terms(rule))


def _four_eyes_terms(rule: StaffRule) -> Iterator[str]:
    """Yield the step id of every four-eyes term in ``rule`` (any depth).

    A term counts when it is ``NODE_PERFORMING_AGENT_SUPERVISOR``, or
    ``NODE_PERFORMING_AGENT`` on an excluding side (an odd number of
    right-hand ``EXCEPT`` operands above it). Terms without ``ref`` are
    skipped. A step referenced twice is yielded twice.

    :param rule: a staff rule.
    :returns: an iterator over referenced step ids, in walk order.
    """

    stack: list[tuple[StaffRule, bool]] = [(rule, False)]
    while stack:
        current, excluding = stack.pop()
        if current.ref and (
            current.kind is StaffRuleKind.NODE_PERFORMING_AGENT_SUPERVISOR
            or (current.kind is StaffRuleKind.NODE_PERFORMING_AGENT and excluding)
        ):
            yield current.ref
        for index, operand in enumerate(current.operands):
            flip = current.kind is StaffRuleKind.EXCEPT and index >= 1
            stack.append((operand, excluding != flip))


def _with_deputies(
    org: OrgModel,
    base: set[str],
    absent: frozenset[str],
    never: frozenset[str] = frozenset(),
    via: dict[str, str] | None = None,
    blocked: dict[str, set[str]] | None = None,
) -> set[str]:
    """Extend an eligible set by the deputies of *absent* agents.

    Follows the substitution chain transitively but only steps across a deputy
    edge when the agent on the near side is currently absent: a present agent
    keeps their own tasks, an absent one hands them to their deputy in parallel.
    If the deputy is in turn absent, their deputy is added too (chain), with a
    visited guard so deputy cycles terminate. The base agents are always kept.

    ``never`` are agents a deputy edge must not lead to -- the performers a
    four-eyes rule is about (:func:`_four_eyes_performers`). They are never
    added. **If such an agent is absent themselves, the chain continues to
    their deputy** (who is checked the same way): the substitution passes the
    excluded person, it does not stop at them -- before, the step stayed
    unstaffed although an admissible deputy existed. A present excluded
    agent ends that branch of the chain.

    ``via``, when given, records for each added deputy the absent agent whose
    deputy edge added it. ``blocked``, when given, records each deputy that
    ``never`` kept out, with the absent agents whose edge led to it.
    """

    result = set(base)
    frontier = list(base)
    passed: set[str] = set()  # excluded agents the chain went past
    while frontier:
        agent = org.agents.get(frontier.pop())
        if agent is None or agent.deputy_id is None:
            continue
        if agent.id not in absent:
            continue
        if agent.deputy_id in never:
            if blocked is not None:
                blocked.setdefault(agent.deputy_id, set()).add(agent.id)
            if agent.deputy_id in absent and agent.deputy_id not in passed:
                passed.add(agent.deputy_id)
                frontier.append(agent.deputy_id)
            continue
        if agent.deputy_id not in result:
            result.add(agent.deputy_id)
            frontier.append(agent.deputy_id)
            if via is not None:
                via[agent.deputy_id] = agent.id
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
