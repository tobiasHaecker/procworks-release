# SPDX-License-Identifier: BUSL-1.1
"""Model metrics and 7PMG-style modelling hints (roadmap E7).

This module is a *read-only* analysis layer. It never changes a schema and it
is **not** part of the correctness validation: the structural rules (Stufe A)
and the release rules (Stufe B) remain the only gates that can reject a model.
The metrics here are advisory editor information ("the model is large", "this
gateway has a high degree") inspired by the Seven Process Modeling Guidelines
(7PMG). They help a modeller keep a schema understandable without ever forcing
a particular style.
"""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel, Field

from procworks.model import (
    JOIN_TYPES,
    SPLIT_TYPES,
    NodeType,
    ProcessSchema,
    ValueClass,
)

#: Advisory thresholds for the modelling hints. Deliberately generous so they
#: flag only genuinely unwieldy models, never ordinary ones.
_SIZE_HINT_THRESHOLD = 50
_DEPTH_HINT_THRESHOLD = 5
_CONNECTOR_DEGREE_HINT_THRESHOLD = 6


class ModelMetrics(BaseModel):
    """Quantitative size/structure metrics of a single schema (7PMG-related)."""

    #: Total number of nodes (7PMG G1: "use as few elements as possible").
    node_count: int
    #: Number of control edges.
    edge_count: int
    #: Number of split/join gateways.
    gateway_count: int
    #: Maximum nesting depth of blocks (7PMG G6: minimise structural depth).
    max_nesting_depth: int
    #: Highest in+out degree among gateways (7PMG G2: low connector degree).
    max_connector_degree: int
    #: Distinct gateway *kinds* in use (1 = homogeneous, 2 = AND and XOR mixed).
    gateway_heterogeneity: int
    #: Number of interactive (ACTIVITY/SUBPROCESS) nodes.
    activity_count: int


class ModelHint(BaseModel):
    """A single non-blocking modelling hint (advisory, never a correctness gate)."""

    #: Stable hint code (``G1``, ``G2``, ``G6`` follow the 7PMG numbering).
    code: str
    #: Human-readable advice.
    message: str
    #: The node the hint refers to, if it is node-local.
    node_id: str | None = None


class ValueClassBreakdown(BaseModel):
    """Aggregated value-adding classification of the activities (roadmap E3)."""

    value_adding: int = 0
    business_necessary: int = 0
    non_value_adding: int = 0
    #: Activities without a value-class annotation yet.
    unclassified: int = 0

    @property
    def classified(self) -> int:
        """Number of activities that carry an explicit value-class."""

        return self.value_adding + self.business_necessary + self.non_value_adding


class ModelReport(BaseModel):
    """The combined, read-only model-analysis report served by the API."""

    metrics: ModelMetrics
    hints: list[ModelHint] = Field(default_factory=list)
    value_classes: ValueClassBreakdown


def _nesting_depth(schema: ProcessSchema) -> int:
    """Maximum block-nesting depth via a topological forward pass.

    The control graph is a balanced, block-structured DAG. Walking it in
    topological order, the running depth increases by one when leaving a split
    (entering a block) and decreases by one when entering a join (closing a
    block). The deepest node's depth is the model's nesting depth.
    """

    nodes = schema.nodes
    if not nodes:
        return 0

    indegree: dict[str, int] = {nid: 0 for nid in nodes}
    succ: dict[str, list[str]] = {nid: [] for nid in nodes}
    for edge in schema.edges:
        if edge.source in nodes and edge.target in nodes:
            succ[edge.source].append(edge.target)
            indegree[edge.target] += 1

    depth: dict[str, int] = {nid: 0 for nid in nodes}
    queue: deque[str] = deque(nid for nid, deg in indegree.items() if deg == 0)
    max_depth = 0
    while queue:
        current = queue.popleft()
        base = depth[current]
        max_depth = max(max_depth, base)
        leaving_split = nodes[current].type in SPLIT_TYPES
        for target in succ[current]:
            entering_join = nodes[target].type in JOIN_TYPES
            candidate = base
            if leaving_split:
                candidate = base + 1
            elif entering_join:
                candidate = max(base - 1, 0)
            depth[target] = max(depth[target], candidate)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return max_depth


def model_metrics(schema: ProcessSchema) -> ModelMetrics:
    """Compute the quantitative model metrics of *schema* (read-only)."""

    gateways = [
        n for n in schema.nodes.values() if n.type in SPLIT_TYPES or n.type in JOIN_TYPES
    ]
    degree: dict[str, int] = {nid: 0 for nid in schema.nodes}
    for edge in schema.edges:
        if edge.source in degree:
            degree[edge.source] += 1
        if edge.target in degree:
            degree[edge.target] += 1
    max_connector_degree = max(
        (degree[n.id] for n in gateways), default=0
    )
    kinds = {
        NodeType.AND_SPLIT if n.type in (NodeType.AND_SPLIT, NodeType.AND_JOIN)
        else NodeType.XOR_SPLIT
        for n in gateways
    }
    activity_count = sum(
        1
        for n in schema.nodes.values()
        if n.type in (NodeType.ACTIVITY, NodeType.SUBPROCESS)
    )
    return ModelMetrics(
        node_count=len(schema.nodes),
        edge_count=len(schema.edges),
        gateway_count=len(gateways),
        max_nesting_depth=_nesting_depth(schema),
        max_connector_degree=max_connector_degree,
        gateway_heterogeneity=len(kinds),
        activity_count=activity_count,
    )


def model_hints(schema: ProcessSchema) -> list[ModelHint]:
    """Derive non-blocking 7PMG-style modelling hints for *schema*.

    These are advisory only. They are intentionally *not* validation findings
    and never influence Stufe A/B correctness.
    """

    metrics = model_metrics(schema)
    hints: list[ModelHint] = []

    if metrics.node_count > _SIZE_HINT_THRESHOLD:
        hints.append(
            ModelHint(
                code="G1",
                message=(
                    f"Das Modell hat {metrics.node_count} Knoten "
                    f"(> {_SIZE_HINT_THRESHOLD}). Eine Zerlegung in Teilprozesse "
                    "verbessert die Verstaendlichkeit."
                ),
            )
        )

    if metrics.max_nesting_depth > _DEPTH_HINT_THRESHOLD:
        hints.append(
            ModelHint(
                code="G6",
                message=(
                    f"Die Verschachtelungstiefe betraegt {metrics.max_nesting_depth} "
                    f"(> {_DEPTH_HINT_THRESHOLD}). Tiefe Schachtelung erschwert das "
                    "Verstaendnis."
                ),
            )
        )

    degree: dict[str, int] = {nid: 0 for nid in schema.nodes}
    for edge in schema.edges:
        if edge.source in degree:
            degree[edge.source] += 1
        if edge.target in degree:
            degree[edge.target] += 1
    for node in schema.nodes.values():
        if node.type not in SPLIT_TYPES and node.type not in JOIN_TYPES:
            continue
        if degree[node.id] >= _CONNECTOR_DEGREE_HINT_THRESHOLD:
            hints.append(
                ModelHint(
                    code="G2",
                    message=(
                        f"Das Gateway '{node.id}' hat einen Grad von "
                        f"{degree[node.id]}. Gateways mit hohem Grad sollten "
                        "aufgeteilt werden."
                    ),
                    node_id=node.id,
                )
            )

    for node in schema.nodes.values():
        if node.type is NodeType.ACTIVITY and not node.label.strip():
            hints.append(
                ModelHint(
                    code="G7",
                    message=(
                        f"Die Aktivitaet '{node.id}' hat kein Label. Sprechende "
                        "Verb-Objekt-Bezeichnungen erhoehen die Verstaendlichkeit."
                    ),
                    node_id=node.id,
                )
            )

    return hints


def value_class_breakdown(schema: ProcessSchema) -> ValueClassBreakdown:
    """Aggregate the value-adding classification of the activities (roadmap E3)."""

    breakdown = ValueClassBreakdown()
    for node in schema.nodes.values():
        if node.type not in (NodeType.ACTIVITY, NodeType.SUBPROCESS):
            continue
        if node.value_class is ValueClass.VALUE_ADDING:
            breakdown.value_adding += 1
        elif node.value_class is ValueClass.BUSINESS_NECESSARY:
            breakdown.business_necessary += 1
        elif node.value_class is ValueClass.NON_VALUE_ADDING:
            breakdown.non_value_adding += 1
        else:
            breakdown.unclassified += 1
    return breakdown


def model_report(schema: ProcessSchema) -> ModelReport:
    """Build the combined model-analysis report (metrics + hints + value mix)."""

    return ModelReport(
        metrics=model_metrics(schema),
        hints=model_hints(schema),
        value_classes=value_class_breakdown(schema),
    )
