# SPDX-License-Identifier: BUSL-1.1
"""Operation preconditions carry a language-neutral code the client can word.

The English ``message`` stays unchanged (logs, API users, tests); ``code`` and
``params`` are additive. Pinned here on real operations, not only on source text.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

import procworks as p
from procworks import add_data_element, create_empty_schema, delete_node, serial_insert
from procworks.model import DataType, ProcessSchema
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


# Every operation that only works on certain node types rejects any other node
# with one ``OP.wrong-node-kind`` finding. Each call targets a node of the wrong
# type (START, or END where the operation sorts its targets first); ``what`` is
# what the client words its message from, so each value is pinned on its own.
_WRONG_KIND_CASES = [
    (
        lambda s: p.set_loop_decision(s, "start", discriminator="N"),
        "loop_decision",
        "start",
        "a loop decision can only be set on a LOOP_END node",
    ),
    (
        lambda s: p.insert_between_node_sets(s, "X", ["start"], ["end"]),
        "sync",
        "end",
        "source/target sets may only contain ACTIVITY nodes",
    ),
    (
        lambda s: p.rename_node(s, "start", "X"),
        "rename",
        "start",
        "only ACTIVITY or SUBPROCESS nodes can be renamed",
    ),
    (
        lambda s: p.move_node(s, "start", "end"),
        "move",
        "start",
        "only ACTIVITY or SUBPROCESS nodes can be moved",
    ),
    (
        lambda s: p.remove_empty_branch(s, "start"),
        "empty_branch",
        "start",
        "only an XOR split can carry an empty branch",
    ),
    (
        lambda s: p.connect_data(s, "start", "N", p.AccessMode.READ),
        "data_access",
        "start",
        "data access is only allowed on ACTIVITY nodes",
    ),
    (
        lambda s: p.set_form(s, "start", fields=[]),
        "input_mask",
        "start",
        "input masks are only allowed on ACTIVITY nodes",
    ),
    (
        lambda s: p.assign_service(s, "start", "svc"),
        "service",
        "start",
        "service can only be bound to ACTIVITY nodes",
    ),
    (
        lambda s: p.assign_staff_rule(
            s, "start", p.StaffRule(kind=p.StaffRuleKind.ROLE, ref="sb")
        ),
        "staff_rule",
        "start",
        "staff rule can only be assigned to ACTIVITY nodes",
    ),
    (
        lambda s: p.convert_activity_to_subprocess(s, "start", "other", 1),
        "subprocess_convert",
        "start",
        "only an ACTIVITY can be converted into a sub-process",
    ),
    (
        lambda s: p.set_subprocess_binding(s, "start", "other", 1),
        "subprocess",
        "start",
        "node is not a SUBPROCESS",
    ),
    (
        lambda s: p.set_value_class(s, "start", None),
        "value_class",
        "start",
        "only ACTIVITY or SUBPROCESS nodes carry a value class",
    ),
    (
        lambda s: p.set_automation(s, "start", p.AutomationKind.MANUAL_NONE),
        "automation",
        "start",
        "automation can only be set on ACTIVITY nodes",
    ),
    (
        lambda s: p.set_node_priority(s, "start", None),
        "priority",
        "start",
        "only ACTIVITY or SUBPROCESS nodes carry a priority",
    ),
    (
        lambda s: p.set_mail_binding(s, "start", None),
        "mail",
        "start",
        "only ACTIVITY nodes can carry a mail notification",
    ),
    (
        lambda s: p.set_time_constraint(s, "start", None),
        "time_constraint",
        "start",
        "only ACTIVITY or SUBPROCESS nodes carry a time constraint",
    ),
]


@pytest.mark.parametrize(
    ("call", "what", "node_id", "message"),
    _WRONG_KIND_CASES,
    ids=[case[1] for case in _WRONG_KIND_CASES],
)
def test_wrong_node_kind_is_coded_per_operation(
    call: Callable[[ProcessSchema], object], what: str, node_id: str, message: str
) -> None:
    s = add_data_element(create_empty_schema("Codes", schema_id="codes-kind"), "N", DataType.STRING)
    with pytest.raises(CorrectnessError) as exc:
        call(s)
    f = _finding(exc)
    assert (f.rule, f.code, f.params) == ("OP", "OP.wrong-node-kind", {"what": what})
    assert f.node_id == node_id
    assert f.message == message
