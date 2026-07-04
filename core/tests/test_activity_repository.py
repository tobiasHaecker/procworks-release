# SPDX-License-Identifier: BUSL-1.1
"""Activity Repository correctness tests (rules A1-A3, Section 6).

A template carries a typed I/O interface and an executor. Binding a template to
an ACTIVITY node must reference an existing template (A1), keep the binding's
``automatic`` flag consistent with the executor (A2), and bind the interface
type-conformantly (A3). Free-form bindings without a template stay valid.
"""

from __future__ import annotations

import pytest

from procworks import (
    ActivityTemplate,
    DataType,
    ExecutorKind,
    ProcessSchema,
    ServiceBinding,
    TemplateParameter,
    add_activity_template,
    add_data_element,
    assign_service,
    create_empty_schema,
    serial_insert,
    validate,
)
from procworks.validator import CorrectnessError


def _activity_id(schema: ProcessSchema, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)


def _schema_with_activity() -> tuple[ProcessSchema, str]:
    schema = create_empty_schema("Repo", schema_id="repo")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    return schema, _activity_id(schema, "Erfassen")


def test_add_activity_template_stores_template() -> None:
    schema, _ = _schema_with_activity()
    schema = add_activity_template(
        schema, "Antrag erfassen", ExecutorKind.MANUAL, template_id="t1"
    )
    assert "t1" in schema.activity_templates
    assert schema.activity_templates["t1"].is_automatic is False


def test_assign_manual_template_is_interactive() -> None:
    schema, act = _schema_with_activity()
    schema = add_activity_template(
        schema, "Erfassen", ExecutorKind.MANUAL, template_id="t1"
    )
    schema = assign_service(schema, act, "Erfassen", template_id="t1")
    # MANUAL templates are interactive: automatic is derived as False.
    assert schema.service_bindings[act].automatic is False
    assert validate(schema) == []


def test_assign_service_template_is_automatic() -> None:
    schema, act = _schema_with_activity()
    schema = add_activity_template(
        schema, "Berechnen", ExecutorKind.SERVICE, template_id="t1"
    )
    schema = assign_service(schema, act, "Berechnen", template_id="t1")
    # SERVICE templates run automatically: automatic is derived as True.
    assert schema.service_bindings[act].automatic is True
    assert validate(schema) == []


def test_assign_unknown_template_rejected_by_operation() -> None:
    schema, act = _schema_with_activity()
    with pytest.raises(CorrectnessError):
        assign_service(schema, act, "X", template_id="ghost")


def test_unknown_template_is_a1() -> None:
    schema, act = _schema_with_activity()
    # Build a binding that references a missing template directly.
    schema.service_bindings[act] = ServiceBinding(
        node_id=act, name="X", template_id="ghost"
    )
    findings = validate(schema)
    assert any(f.rule == "A1" for f in findings)


def test_automatic_mismatch_is_a2() -> None:
    schema, act = _schema_with_activity()
    schema.activity_templates["t1"] = ActivityTemplate(
        id="t1", name="Erfassen", executor=ExecutorKind.MANUAL
    )
    # MANUAL template but binding claims automatic -> A2.
    schema.service_bindings[act] = ServiceBinding(
        node_id=act, name="Erfassen", automatic=True, template_id="t1"
    )
    findings = validate(schema)
    assert any(f.rule == "A2" for f in findings)


def test_conformant_interface_ok() -> None:
    schema, act = _schema_with_activity()
    schema = add_data_element(schema, "betrag", DataType.FLOAT, element_id="betrag")
    schema = add_activity_template(
        schema,
        "Pruefen",
        ExecutorKind.SERVICE,
        inputs=[TemplateParameter(name="wert", data_type=DataType.FLOAT)],
        template_id="t1",
    )
    schema = assign_service(
        schema, act, "Pruefen", template_id="t1", parameter_mapping={"wert": "betrag"}
    )
    assert validate(schema) == []


def test_mandatory_parameter_not_bound_is_a3() -> None:
    schema, act = _schema_with_activity()
    schema = add_activity_template(
        schema,
        "Pruefen",
        ExecutorKind.SERVICE,
        inputs=[TemplateParameter(name="wert", data_type=DataType.FLOAT)],
        template_id="t1",
    )
    with pytest.raises(CorrectnessError) as exc:
        assign_service(schema, act, "Pruefen", template_id="t1")
    assert any(f.rule == "A3" for f in exc.value.findings)


def test_parameter_type_mismatch_is_a3() -> None:
    schema, act = _schema_with_activity()
    schema = add_data_element(schema, "name", DataType.STRING, element_id="name")
    schema = add_activity_template(
        schema,
        "Pruefen",
        ExecutorKind.SERVICE,
        inputs=[TemplateParameter(name="wert", data_type=DataType.FLOAT)],
        template_id="t1",
    )
    with pytest.raises(CorrectnessError) as exc:
        assign_service(
            schema, act, "Pruefen", template_id="t1", parameter_mapping={"wert": "name"}
        )
    assert any(f.rule == "A3" for f in exc.value.findings)


def test_unknown_parameter_name_is_a3() -> None:
    schema, act = _schema_with_activity()
    schema = add_data_element(schema, "betrag", DataType.FLOAT, element_id="betrag")
    schema = add_activity_template(
        schema, "Pruefen", ExecutorKind.SERVICE, template_id="t1"
    )
    with pytest.raises(CorrectnessError) as exc:
        assign_service(
            schema, act, "Pruefen", template_id="t1", parameter_mapping={"x": "betrag"}
        )
    assert any(f.rule == "A3" for f in exc.value.findings)


def test_free_form_binding_without_template_stays_valid() -> None:
    schema, act = _schema_with_activity()
    schema = assign_service(schema, act, "Freie Bindung", automatic=True)
    assert schema.service_bindings[act].template_id is None
    assert validate(schema) == []
