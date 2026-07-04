# SPDX-License-Identifier: BUSL-1.1
"""Input-mask / form-designer tests (rules U1-U3 and the CbC bridge to D1)."""

from __future__ import annotations

import pytest

from procworks import (
    BranchSpec,
    FormFieldSpec,
    add_data_element,
    conditional_insert,
    connect_data,
    create_empty_schema,
    delete_form,
    delete_node,
    disconnect_data,
    export_bpmn,
    import_bpmn,
    new_revision,
    release,
    serial_insert,
    set_form,
    validate,
)
from procworks.model import AccessMode, DataType, WidgetKind
from procworks.validator import CorrectnessError


def _activity_ids(schema, label):
    return [n.id for n in schema.nodes.values() if n.label == label]


def _linear_with_element(schema_id="form"):
    """START -> Erfassen -> END plus a STRING element ``name``."""

    schema = create_empty_schema("Maske", schema_id=schema_id)
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    node = _activity_ids(schema, "Erfassen")[0]
    schema = add_data_element(schema, "Name", DataType.STRING, element_id="name")
    return schema, node


def test_set_form_creates_field_and_write_access():
    schema, node = _linear_with_element()
    schema = set_form(
        schema,
        node,
        title="Antrag",
        fields=[FormFieldSpec(element_id="name", widget=WidgetKind.TEXT)],
    )

    assert validate(schema) == []
    form = schema.forms[node]
    assert form.title == "Antrag"
    assert [f.element_id for f in form.fields] == ["name"]
    # The field defaults its label from the element name and is auto-backed by a
    # write access (rule U3).
    assert form.fields[0].label == "Name"
    modes = {a.mode for a in schema.accesses_of(node) if a.element_id == "name"}
    assert AccessMode.WRITE in modes


def test_dropdown_requires_at_least_two_unique_options():
    schema, node = _linear_with_element()
    with pytest.raises(CorrectnessError) as exc:
        set_form(
            schema,
            node,
            fields=[
                FormFieldSpec(
                    element_id="name",
                    widget=WidgetKind.DROPDOWN,
                    options=("nur eins",),
                )
            ],
        )
    assert any(f.rule == "U2" for f in exc.value.findings)


def test_widget_type_mismatch_is_rejected():
    schema, node = _linear_with_element()
    # A STRING element cannot be presented by a CHECKBOX widget.
    with pytest.raises(CorrectnessError) as exc:
        set_form(
            schema,
            node,
            fields=[FormFieldSpec(element_id="name", widget=WidgetKind.CHECKBOX)],
        )
    assert any(f.rule == "U2" for f in exc.value.findings)


def test_form_only_on_activity():
    schema, _ = _linear_with_element()
    with pytest.raises(CorrectnessError) as exc:
        set_form(
            schema,
            "start",
            fields=[FormFieldSpec(element_id="name", widget=WidgetKind.TEXT)],
        )
    assert any(f.rule == "OP" for f in exc.value.findings)


def test_unknown_element_rejected():
    schema, node = _linear_with_element()
    with pytest.raises(CorrectnessError) as exc:
        set_form(
            schema,
            node,
            fields=[FormFieldSpec(element_id="ghost", widget=WidgetKind.TEXT)],
        )
    assert any(f.rule == "OP" for f in exc.value.findings)


def test_element_bound_twice_rejected():
    schema, node = _linear_with_element()
    with pytest.raises(CorrectnessError) as exc:
        set_form(
            schema,
            node,
            fields=[
                FormFieldSpec(element_id="name", widget=WidgetKind.TEXT),
                FormFieldSpec(element_id="name", widget=WidgetKind.TEXTAREA),
            ],
        )
    assert any(f.rule == "OP" for f in exc.value.findings)


def test_read_field_needs_prior_write_on_all_paths():
    """CbC: a mask that displays an element not written on every path -> D1.

    ``extra`` is written only in one XOR branch, so a downstream mask that reads
    it is rejected -- kein Read ohne Set, even in complex, branched models.
    """

    schema = create_empty_schema("Verzweigt", schema_id="uxor")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erfassen = _activity_ids(schema, "Erfassen")[0]
    schema = add_data_element(schema, "flag", DataType.INTEGER, element_id="flag")
    schema = connect_data(schema, erfassen, "flag", AccessMode.WRITE)
    schema = conditional_insert(
        schema,
        after_node_id=erfassen,
        discriminator="flag",
        branches=[BranchSpec(label="Zweig A", upper=1), BranchSpec(label="Zweig B")],
    )
    branch_a = _activity_ids(schema, "Zweig A")[0]
    schema = add_data_element(schema, "Extra", DataType.STRING, element_id="extra")
    # Written only in branch A.
    schema = connect_data(schema, branch_a, "extra", AccessMode.WRITE)

    join = next(
        n.id for n in schema.nodes.values() if n.type.name == "XOR_JOIN"
    )
    schema = serial_insert(schema, "Pruefen", after_node_id=join)
    pruefen = _activity_ids(schema, "Pruefen")[0]

    with pytest.raises(CorrectnessError) as exc:
        set_form(
            schema,
            pruefen,
            fields=[
                FormFieldSpec(
                    element_id="extra",
                    widget=WidgetKind.TEXT,
                    mode=AccessMode.READ,
                )
            ],
        )
    assert any(f.rule == "D1" for f in exc.value.findings)


def test_read_field_ok_when_written_before():
    schema, writer = _linear_with_element(schema_id="uok")
    schema = set_form(
        schema,
        writer,
        fields=[FormFieldSpec(element_id="name", widget=WidgetKind.TEXT)],
    )
    schema = serial_insert(schema, "Anzeigen", after_node_id=writer)
    display = _activity_ids(schema, "Anzeigen")[0]
    schema = set_form(
        schema,
        display,
        fields=[
            FormFieldSpec(
                element_id="name", widget=WidgetKind.TEXT, mode=AccessMode.READ
            )
        ],
    )
    assert validate(schema) == []


def test_delete_form_removes_managed_access():
    schema, node = _linear_with_element()
    schema = set_form(
        schema,
        node,
        fields=[FormFieldSpec(element_id="name", widget=WidgetKind.TEXT)],
    )
    schema = delete_form(schema, node)
    assert node not in schema.forms
    assert not any(a.element_id == "name" for a in schema.accesses_of(node))


def test_delete_node_removes_form():
    schema, node = _linear_with_element()
    schema = set_form(
        schema,
        node,
        fields=[FormFieldSpec(element_id="name", widget=WidgetKind.TEXT)],
    )
    schema = delete_node(schema, node)
    assert node not in schema.forms


def test_new_revision_carries_form():
    schema, node = _linear_with_element()
    schema = set_form(
        schema,
        node,
        fields=[FormFieldSpec(element_id="name", widget=WidgetKind.TEXT)],
    )
    schema = release(schema)
    revised = new_revision(schema)
    assert node in revised.forms


def test_bpmn_round_trip_preserves_form():
    schema, node = _linear_with_element(schema_id="ubpmn")
    schema = set_form(
        schema,
        node,
        title="Erfassung",
        fields=[
            FormFieldSpec(
                element_id="name",
                widget=WidgetKind.DROPDOWN,
                options=("A", "B"),
            )
        ],
    )
    restored = import_bpmn(export_bpmn(schema))
    form = restored.forms[node]
    assert form.title == "Erfassung"
    assert form.fields[0].widget is WidgetKind.DROPDOWN
    assert form.fields[0].options == ["A", "B"]


def test_disconnect_data_removes_backing_mask_field():
    """Removing a data binding also drops the input-mask field it backed, so
    mask and data flow stay consistent (U3). A mask left empty is removed."""
    schema, node = _linear_with_element("disc-mask")
    schema = set_form(
        schema,
        node,
        title="Antrag",
        fields=[FormFieldSpec(element_id="name", widget=WidgetKind.TEXT)],
    )
    assert node in schema.forms

    schema = disconnect_data(schema, node, "name")

    assert not any(
        a.node_id == node and a.element_id == "name" for a in schema.data_accesses
    )
    # The only field vanished, so the whole mask is gone.
    assert node not in schema.forms
    assert validate(schema) == []
