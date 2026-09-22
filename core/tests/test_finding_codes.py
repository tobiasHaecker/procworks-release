# SPDX-License-Identifier: BUSL-1.1
"""Operation preconditions carry a language-neutral code the client can word.

The English ``message`` stays unchanged (logs, API users, tests); ``code`` and
``params`` are additive. Pinned here on real operations, not only on source text.
"""

from __future__ import annotations

import pytest

from procworks import add_data_element, create_empty_schema, delete_node, serial_insert
from procworks.model import DataType
from procworks.operations import add_role, set_form
from procworks.validator import CorrectnessError


def _finding(exc: pytest.ExceptionInfo[CorrectnessError]):  # type: ignore[no-untyped-def]
    [f] = exc.value.findings
    return f


def test_deleting_start_is_coded() -> None:
    s = create_empty_schema("Codes", schema_id="codes-1")
    with pytest.raises(CorrectnessError) as exc:
        delete_node(s, "start")
    f = _finding(exc)
    assert (f.rule, f.code) == ("OP", "OP.delete-start-end")
    assert f.message == "cannot delete START or END"  # unchanged technical text


def test_not_found_names_kind_and_id() -> None:
    s = create_empty_schema("Codes", schema_id="codes-2")
    with pytest.raises(CorrectnessError) as exc:
        serial_insert(s, "X", after_node_id="gibt-es-nicht")
    f = _finding(exc)
    assert f.code == "OP.not-found"
    assert f.params == {"kind": "node", "name": "gibt-es-nicht"}


def test_duplicate_role_is_coded() -> None:
    s = add_role(create_empty_schema("Codes", schema_id="codes-3"), "SB", role_id="sb")
    with pytest.raises(CorrectnessError) as exc:
        add_role(s, "SB", role_id="sb")
    f = _finding(exc)
    assert f.code == "OP.already-exists" and f.params == {"kind": "role", "name": "sb"}


def test_mask_on_a_gateway_is_a_wrong_node_kind() -> None:
    s = add_data_element(create_empty_schema("Codes", schema_id="codes-4"), "N", DataType.STRING)
    with pytest.raises(CorrectnessError) as exc:
        set_form(s, "start", fields=[])
    f = _finding(exc)
    assert f.code == "OP.wrong-node-kind" and f.params == {"what": "input_mask"}
