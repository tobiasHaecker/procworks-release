# SPDX-License-Identifier: BUSL-1.1
"""Tests for read-only model metrics and 7PMG hints (roadmap E7).

The metrics are an advisory analysis layer: they never change a schema and are
never part of the Stufe-A/B correctness gates. These tests pin the computed
figures (size, nesting depth, gateway heterogeneity, connector degree) and the
non-blocking hint behaviour.
"""

from __future__ import annotations

from procworks import (
    AccessMode,
    BranchSpec,
    DataType,
    add_data_element,
    conditional_insert,
    connect_data,
    create_empty_schema,
    model_hints,
    model_metrics,
    model_report,
    parallel_insert,
    serial_insert,
    set_value_class,
    value_class_breakdown,
)
from procworks.metrics import _nesting_depth
from procworks.model import NodeType, ValueClass


def _activity_id(schema, label):
    return next(n.id for n in schema.nodes.values() if n.label == label)


def test_metrics_of_minimal_schema() -> None:
    schema = create_empty_schema("Leer", schema_id="m0")
    metrics = model_metrics(schema)
    assert metrics.node_count == 2  # START + END
    assert metrics.edge_count == 1
    assert metrics.gateway_count == 0
    assert metrics.max_nesting_depth == 0
    assert metrics.max_connector_degree == 0
    assert metrics.gateway_heterogeneity == 0
    assert metrics.activity_count == 0


def test_metrics_counts_activities_and_gateways() -> None:
    schema = create_empty_schema("Block", schema_id="m1")
    schema = serial_insert(schema, "A", after_node_id="start")
    schema = parallel_insert(schema, ["P1", "P2"], after_node_id="start")
    metrics = model_metrics(schema)
    # 2 START/END + 1 serial activity + 2 parallel activities + AND split/join.
    assert metrics.activity_count == 3
    assert metrics.gateway_count == 2
    assert metrics.gateway_heterogeneity == 1  # only AND gateways
    assert metrics.max_nesting_depth == 1


def test_metrics_gateway_heterogeneity_and_depth() -> None:
    schema = create_empty_schema("Mix", schema_id="m2")
    schema = parallel_insert(schema, ["P1", "P2"], after_node_id="start")
    p1 = _activity_id(schema, "P1")
    schema = add_data_element(schema, "x", DataType.INTEGER, element_id="x")
    schema = connect_data(schema, p1, "x", AccessMode.WRITE)
    schema = conditional_insert(
        schema,
        after_node_id=p1,
        discriminator="x",
        branches=[BranchSpec(label="C1", upper=1), BranchSpec(label="C2")],
    )
    metrics = model_metrics(schema)
    assert metrics.gateway_heterogeneity == 2  # AND and XOR both present
    assert metrics.max_nesting_depth == 2  # XOR block nested in AND branch


def test_nesting_depth_is_balanced() -> None:
    schema = create_empty_schema("Tief", schema_id="m3")
    schema = parallel_insert(schema, ["P1", "P2"], after_node_id="start")
    # The AND_JOIN closes the block, so the END node is back at depth 0.
    end_id = schema.end_node().id
    depth = _nesting_depth(schema)
    assert depth == 1
    assert schema.nodes[end_id].type is NodeType.END


def test_hints_flag_large_model() -> None:
    schema = create_empty_schema("Gross", schema_id="m4")
    anchor = "start"
    for i in range(60):
        schema = serial_insert(schema, f"A{i}", after_node_id=anchor)
        anchor = _activity_id(schema, f"A{i}")
    codes = {h.code for h in model_hints(schema)}
    assert "G1" in codes  # size hint


def test_hints_empty_for_small_model() -> None:
    schema = create_empty_schema("Klein", schema_id="m5")
    schema = serial_insert(schema, "A", after_node_id="start")
    assert model_hints(schema) == []


def test_value_class_breakdown_aggregates() -> None:
    schema = create_empty_schema("Wert", schema_id="m6")
    schema = serial_insert(schema, "A", after_node_id="start")
    schema = serial_insert(schema, "B", after_node_id="start")
    a = _activity_id(schema, "A")
    b = _activity_id(schema, "B")
    schema = set_value_class(schema, a, ValueClass.VALUE_ADDING)
    schema = set_value_class(schema, b, ValueClass.NON_VALUE_ADDING)
    breakdown = value_class_breakdown(schema)
    assert breakdown.value_adding == 1
    assert breakdown.non_value_adding == 1
    assert breakdown.business_necessary == 0
    assert breakdown.unclassified == 0
    assert breakdown.classified == 2


def test_model_report_combines_everything() -> None:
    schema = create_empty_schema("Report", schema_id="m7")
    schema = serial_insert(schema, "A", after_node_id="start")
    report = model_report(schema)
    assert report.metrics.activity_count == 1
    assert report.hints == []
    assert report.value_classes.unclassified == 1
