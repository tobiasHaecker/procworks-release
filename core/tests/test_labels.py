# SPDX-License-Identifier: BUSL-1.1
"""Names and labels are trimmed and bounded (Validierung 2026-09-25, VAL-29).

Labels that were empty, only blanks or 5000 characters long were accepted.
Every operation that sets a name trims it and rejects an empty or overlong one
(``OP.label-empty`` / ``OP.label-too-long``); the length bound is also part of
``validate()`` (U6), so the BPMN import honours it.
"""

from __future__ import annotations

import pytest

from procworks import (
    add_data_element,
    add_role,
    create_empty_schema,
    rename_node,
    serial_insert,
)
from procworks.bpmn import import_bpmn
from procworks.model import DataType
from procworks.validator import MAX_LABEL_LENGTH, CorrectnessError


def _codes(exc: pytest.ExceptionInfo[CorrectnessError]) -> list[str | None]:
    return [f.code for f in exc.value.findings]


@pytest.mark.parametrize("label", ["", "   ", "\t\n"])
def test_blank_step_labels_are_rejected(label: str) -> None:
    schema = create_empty_schema("Leer", schema_id="blank")
    with pytest.raises(CorrectnessError) as exc:
        serial_insert(schema, label, after_node_id="start")
    assert _codes(exc) == ["OP.label-empty"]


def test_labels_are_trimmed() -> None:
    schema = serial_insert(create_empty_schema("Trim", schema_id="trim"), "  Prüfen  ", "start")
    assert "Prüfen" in {n.label for n in schema.nodes.values()}


def test_overlong_names_are_rejected_everywhere() -> None:
    long = "x" * (MAX_LABEL_LENGTH + 1)
    schema = serial_insert(create_empty_schema("Lang", schema_id="long"), "A", "start")
    act = next(n.id for n in schema.nodes.values() if n.label == "A")
    for attempt in (
        lambda: rename_node(schema, act, long),
        lambda: add_data_element(schema, long, DataType.STRING),
        lambda: add_role(schema, long),
        lambda: create_empty_schema(long),
    ):
        with pytest.raises(CorrectnessError) as exc:
            attempt()
        assert _codes(exc) == ["OP.label-too-long"]


def test_exactly_the_maximum_is_fine() -> None:
    schema = create_empty_schema("Grenze", schema_id="edge")
    serial_insert(schema, "y" * MAX_LABEL_LENGTH, after_node_id="start")


def test_bpmn_import_honours_the_length_bound() -> None:
    ns = 'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"'
    xml = (
        f'<bpmn:definitions {ns}><bpmn:process id="p">'
        '<bpmn:startEvent id="s"/>'
        f'<bpmn:task id="a" name="{"z" * 5000}"/><bpmn:endEvent id="e"/>'
        '<bpmn:sequenceFlow id="1" sourceRef="s" targetRef="a"/>'
        '<bpmn:sequenceFlow id="2" sourceRef="a" targetRef="e"/>'
        "</bpmn:process></bpmn:definitions>"
    )
    with pytest.raises(CorrectnessError) as exc:
        import_bpmn(xml, schema_id="long-import")
    assert "U6.label-too-long" in _codes(exc)
