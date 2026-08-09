# SPDX-License-Identifier: BUSL-1.1
"""Read-only what-if simulation of one schema run (roadmap E6).

A **pure** module like ``metrics.py``: :func:`simulate` drives a throw-away
in-memory instance through the *pure* engine -- no context, no store, no
audit, no mail, no external tasks, no clock stamps -- so the token walk uses
exactly the operational semantics while being strictly side-effect free
(Simulations-Konzept §2). The result is a synthetic marking view the web
client renders with the same graph as every runtime view, plus the chosen
XOR branches, the expected duration over the executed path and advisory
findings (German, like the model hints -- user-facing aid, never a verdict).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from procworks import execution as exe
from procworks.model import (
    EdgeType,
    InstanceState,
    NodeState,
    NodeType,
    ProcessInstance,
    ProcessSchema,
    loop_block,
)

#: Default round cap for loops: with static seeded values a REPEAT-UNTIL
#: either exits at once or repeats forever, so the simulation cuts off
#: honestly after this many total iterations (Simulations-Konzept §3).
MAX_LOOP_ITERATIONS = 25


class SimulationResult(BaseModel):
    """Outcome of one side-effect-free token walk (never persisted)."""

    #: True when the walk reached the END node with the seeded values.
    completed: bool
    #: Completion order of the interactive/automatic work steps.
    executed: list[str] = Field(default_factory=list)
    #: Final node marking -- a synthetic instance view for rendering.
    node_states: dict[str, NodeState] = Field(default_factory=dict)
    #: Chosen XOR branch (split node id -> chosen target node id).
    decisions: dict[str, str] = Field(default_factory=dict)
    #: Simulated repeat counts per LOOP_END node id.
    loop_iterations: dict[str, int] = Field(default_factory=dict)
    #: Data values at the end of the walk (the seeded values).
    data_values: dict[str, object] = Field(default_factory=dict)
    #: Longest target-duration path over the *executed* nodes (parallel
    #: branches count as their maximum, loop bodies multiplied by their
    #: simulated rounds), or ``None`` without time annotations.
    expected_duration_seconds: float | None = None
    #: Advisory findings (abort reason, loop cap) -- German, display-only.
    findings: list[str] = Field(default_factory=list)


def simulate(
    schema: ProcessSchema,
    data: dict[str, object] | None = None,
    *,
    loop_cap: int = MAX_LOOP_ITERATIONS,
) -> SimulationResult:
    """Run one deterministic, side-effect-free token walk (E6).

    Seeds a throw-away instance with ``data`` (drafts allowed -- semantic
    validation happens while modelling), then repeatedly completes the
    alphabetically first ready activity: deterministic and reproducible; the
    interleaving of parallel branches affects neither path nor duration. The
    walk assumes steps deliver exactly the seeded values (static); a missing
    decision value aborts with a clear finding -- which *is* the insight.
    """

    label_of = _labeller(schema)
    try:
        instance = exe.instantiate(
            schema,
            "simulation",
            allow_unreleased=True,
            is_test=True,
            initial_data=dict(data or {}),
        )
    except exe.ExecutionError as err:
        return SimulationResult(
            completed=False,
            findings=[f"Abbruch beim Start: {err.message}"],
        )

    executed: list[str] = []
    findings: list[str] = []
    # Hard guard against any unforeseen non-progress: generous, never the
    # mechanism that ends a loop (the loop cap below reports first).
    max_steps = max(1, len(schema.nodes)) * (loop_cap + 2)
    while instance.state is InstanceState.RUNNING:
        ready = sorted(exe.worklist(instance, schema))
        if not ready:
            findings.append(
                "Die Simulation kommt nicht weiter: keine bereite Aktivität "
                "(der Vorgang wartet, z. B. auf einen Teilprozess)."
            )
            break
        node_id = ready[0]
        if len(executed) >= max_steps:  # pragma: no cover - loop cap fires first
            findings.append("Abbruch: Schrittlimit der Simulation erreicht.")
            break
        try:
            instance = exe.complete_activity(instance, schema, node_id)
        except exe.ExecutionError as err:
            findings.append(f"Abbruch bei „{label_of(node_id)}“: {err.message}")
            break
        executed.append(node_id)
        if sum(instance.loop_iterations.values()) > loop_cap:
            findings.append(
                f"Schleifen-Abbruch: Mit diesen Werten wiederholt der Prozess "
                f"endlos (nach {loop_cap} Runden abgebrochen). Prüfen Sie das "
                f"Wiederholen-Merkmal oder setzen Sie eine Höchstzahl an "
                f"Durchläufen."
            )
            break

    return SimulationResult(
        completed=instance.state is InstanceState.COMPLETED,
        executed=executed,
        node_states=dict(instance.node_states),
        decisions=dict(instance.decisions),
        loop_iterations=dict(instance.loop_iterations),
        data_values=dict(instance.data_values),
        expected_duration_seconds=_expected_duration(schema, instance),
        findings=findings,
    )


def _labeller(schema: ProcessSchema):  # type: ignore[no-untyped-def]
    def label_of(node_id: str) -> str:
        node = schema.nodes.get(node_id)
        return (node.label or node_id) if node is not None else node_id

    return label_of


def _expected_duration(
    schema: ProcessSchema, instance: ProcessInstance
) -> float | None:
    """Longest target-duration path over the executed (COMPLETED) nodes.

    The runtime twin of the validator's T2 critical path, restricted to the
    walk that actually happened: skipped branches drop out, parallel branches
    contribute their maximum, and loop-block nodes are multiplied by their
    *simulated* round count (unlike T2, which needs the modelled
    ``max_iterations`` bound at design time). ``None`` without any time
    annotation -- no fake precision.
    """

    if not schema.time_constraints:
        return None

    multiplier: dict[str, float] = {}
    for nid, node in schema.nodes.items():
        if node.type is not NodeType.LOOP_START:
            continue
        try:
            end_id, body = loop_block(schema, nid)
        except ValueError:  # pragma: no cover - K6a-valid schemas only
            continue
        rounds = instance.loop_iterations.get(end_id, 0) + 1
        if rounds > 1:
            for member in body | {nid, end_id}:
                multiplier[member] = multiplier.get(member, 1.0) * rounds

    def duration(node_id: str) -> float:
        constraint = schema.time_constraints.get(node_id)
        base = 0.0
        if constraint is not None and constraint.max_duration_seconds is not None:
            base = constraint.max_duration_seconds
        return base * multiplier.get(node_id, 1.0)

    completed = {
        nid
        for nid, state in instance.node_states.items()
        if state is NodeState.COMPLETED
    }
    if not completed:
        return 0.0

    indegree = {nid: 0 for nid in completed}
    succ: dict[str, list[str]] = {nid: [] for nid in completed}
    for edge in schema.edges:
        if edge.type is not EdgeType.CONTROL:
            continue  # SYNC (K4) is ordering-only (consistent with T2)
        if edge.source in completed and edge.target in completed:
            succ[edge.source].append(edge.target)
            indegree[edge.target] += 1

    longest: dict[str, float] = {}
    queue = [nid for nid, deg in indegree.items() if deg == 0]
    while queue:
        current = queue.pop()
        best = max(
            (
                longest[p]
                for p, targets in succ.items()
                if current in targets and p in longest
            ),
            default=0.0,
        )
        longest[current] = best + duration(current)
        for target in succ[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return max(longest.values(), default=0.0)
