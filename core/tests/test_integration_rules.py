# SPDX-License-Identifier: BUSL-1.1
"""Integration rules I1-I4 for automatic, tool-driven service bindings (E11).

An automatic ACTIVITY can be driven from the integration boundary either by an
external worker that pulls an external task (``EXTERNAL_TASK`` + topic) or by a
server-side push to a referenced endpoint (``HTTP_PUSH`` + endpoint_ref). The
rules keep such a binding well-formed (I1), internally consistent (I2),
referentially intact (I3) and free of inline secrets (I4). They are fully
additive: a model without an automation binding produces no integration
finding.
"""

from __future__ import annotations

import pytest

from procworks import (
    AutomationKind,
    DataType,
    ServiceBinding,
    add_data_element,
    assign_service,
    create_empty_schema,
    serial_insert,
    set_automation,
    validate,
)
from procworks.validator import CorrectnessError


def _activity_ids(schema, label):
    return [n.id for n in schema.nodes.values() if n.label == label]


def _automatic_activity():
    """A draft schema with one automatic ACTIVITY that already has a binding."""

    schema = create_empty_schema("Integration", schema_id="int")
    schema = serial_insert(schema, "Sync", after_node_id="start")
    act = _activity_ids(schema, "Sync")[0]
    schema = assign_service(schema, act, "Sync-Dienst", automatic=True)
    return schema, act


# --- additivity: no automation means no integration finding --------------


def test_models_without_automation_have_no_integration_finding() -> None:
    schema, _act = _automatic_activity()
    findings = validate(schema)
    assert [f for f in findings if f.rule.startswith("I")] == []


# --- I1/I2: well-formed external-task and http-push bindings -------------


def test_external_task_with_topic_is_valid() -> None:
    schema, act = _automatic_activity()
    schema = set_automation(
        schema, act, AutomationKind.EXTERNAL_TASK, topic="invoices.sync"
    )
    binding = schema.service_bindings[act]
    assert binding.automation is AutomationKind.EXTERNAL_TASK
    assert binding.topic == "invoices.sync"
    assert binding.automatic is True
    assert [f for f in validate(schema) if f.rule.startswith("I")] == []


def test_http_push_with_endpoint_is_valid() -> None:
    schema, act = _automatic_activity()
    schema = set_automation(
        schema, act, AutomationKind.HTTP_PUSH, endpoint_ref="erp.create-order"
    )
    binding = schema.service_bindings[act]
    assert binding.automation is AutomationKind.HTTP_PUSH
    assert binding.endpoint_ref == "erp.create-order"
    assert [f for f in validate(schema) if f.rule.startswith("I")] == []


def test_external_task_without_topic_is_rejected() -> None:
    schema, act = _automatic_activity()
    with pytest.raises(CorrectnessError) as exc:
        set_automation(schema, act, AutomationKind.EXTERNAL_TASK)
    assert any(f.rule == "I1" for f in exc.value.findings)


def test_http_push_without_endpoint_is_rejected() -> None:
    schema, act = _automatic_activity()
    with pytest.raises(CorrectnessError) as exc:
        set_automation(schema, act, AutomationKind.HTTP_PUSH)
    assert any(f.rule == "I1" for f in exc.value.findings)


def test_external_task_must_not_set_endpoint() -> None:
    schema, act = _automatic_activity()
    with pytest.raises(CorrectnessError) as exc:
        set_automation(
            schema,
            act,
            AutomationKind.EXTERNAL_TASK,
            topic="invoices.sync",
            endpoint_ref="erp.create-order",
        )
    assert any(f.rule == "I2" for f in exc.value.findings)


def test_inconsistent_binding_flags_i2() -> None:
    # A directly constructed binding that declares automation but is not marked
    # automatic violates I2 (an automated binding is never interactive).
    schema, act = _automatic_activity()
    schema = schema.model_copy(deep=True)
    schema.service_bindings[act] = ServiceBinding(
        node_id=act,
        name="Sync-Dienst",
        automatic=False,
        automation=AutomationKind.EXTERNAL_TASK,
        topic="invoices.sync",
    )
    findings = validate(schema)
    assert any(f.rule == "I2" for f in findings)


# --- I3: parameter mapping must reference existing data elements ---------


def test_parameter_mapping_to_unknown_element_flags_i3() -> None:
    schema, act = _automatic_activity()
    schema = schema.model_copy(deep=True)
    binding = schema.service_bindings[act]
    binding.automation = AutomationKind.EXTERNAL_TASK
    binding.topic = "invoices.sync"
    binding.parameter_mapping = {"amount": "missing-element"}
    findings = validate(schema)
    assert any(f.rule == "I3" for f in findings)


def test_parameter_mapping_to_existing_element_is_valid() -> None:
    schema, act = _automatic_activity()
    schema = add_data_element(schema, "Betrag", DataType.INTEGER, element_id="amount")
    schema = schema.model_copy(deep=True)
    binding = schema.service_bindings[act]
    binding.automation = AutomationKind.EXTERNAL_TASK
    binding.topic = "invoices.sync"
    binding.parameter_mapping = {"amount": "amount"}
    assert [f for f in validate(schema) if f.rule == "I3"] == []


# --- I4: no inline secrets in reference fields ---------------------------


def test_endpoint_with_inline_credentials_is_rejected() -> None:
    schema, act = _automatic_activity()
    with pytest.raises(CorrectnessError) as exc:
        set_automation(
            schema,
            act,
            AutomationKind.HTTP_PUSH,
            endpoint_ref="https://user:pass@erp.example.com/orders",
        )
    assert any(f.rule == "I4" for f in exc.value.findings)


def test_topic_with_url_scheme_is_rejected() -> None:
    schema, act = _automatic_activity()
    with pytest.raises(CorrectnessError) as exc:
        set_automation(
            schema, act, AutomationKind.EXTERNAL_TASK, topic="amqp://broker/queue"
        )
    assert any(f.rule == "I4" for f in exc.value.findings)


# --- operation guards ----------------------------------------------------


def test_set_automation_requires_service_binding() -> None:
    schema = create_empty_schema("Integration", schema_id="int")
    schema = serial_insert(schema, "Sync", after_node_id="start")
    act = _activity_ids(schema, "Sync")[0]
    with pytest.raises(CorrectnessError) as exc:
        set_automation(schema, act, AutomationKind.EXTERNAL_TASK, topic="t")
    assert any(f.rule == "OP" for f in exc.value.findings)


def test_set_automation_rejects_non_activity_node() -> None:
    schema, _act = _automatic_activity()
    with pytest.raises(CorrectnessError) as exc:
        set_automation(schema, "start", AutomationKind.EXTERNAL_TASK, topic="t")
    assert any(f.rule == "OP" for f in exc.value.findings)
