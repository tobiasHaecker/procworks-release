# SPDX-License-Identifier: BUSL-1.1
"""Tests for the Execution Engine (roadmap step 8).

These verify the runtime marking semantics: a RELEASED schema is instantiated,
activities become ready in the right order, parallel (AND) branches run
concurrently, an XOR branch is resolved automatically from the instance data
(K7) and the unchosen branch is skipped, and a finished instance ends with
every node COMPLETED or SKIPPED.
"""

from __future__ import annotations

import pytest

from procworks import (
    AccessMode,
    BranchSpec,
    DataType,
    add_data_element,
    complete_activity,
    conditional_insert,
    connect_data,
    create_empty_schema,
    instantiate,
    parallel_insert,
    pending_decisions,
    release,
    serial_insert,
    start_activity,
    worklist,
)
from procworks.execution import ExecutionError
from procworks.model import InstanceState, NodeState, NodeType


def _nid(schema: object, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)  # type: ignore[attr-defined]


def _released_serial() -> object:
    schema = create_empty_schema("Seriell")
    schema = serial_insert(schema, "B", after_node_id="start")
    schema = serial_insert(schema, "A", after_node_id="start")
    return release(schema)


def _released_conditional() -> object:
    """A released schema whose XOR split is driven by an INTEGER discriminator.

    "Erfassen" writes ``betrag``; the split partitions it into ``< 1001`` (Team)
    and ``>= 1001`` (Leitung), so the branch is decided purely from the data.
    """

    schema = create_empty_schema("Bedingt")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    erfassen = _nid(schema, "Erfassen")
    schema = add_data_element(schema, "Betrag", DataType.INTEGER, element_id="betrag")
    schema = connect_data(schema, erfassen, "betrag", AccessMode.WRITE)
    schema = conditional_insert(
        schema,
        after_node_id=erfassen,
        discriminator="betrag",
        branches=[
            BranchSpec(label="Team", upper=1001),
            BranchSpec(label="Leitung"),
        ],
    )
    return release(schema)


def test_instantiate_requires_released() -> None:
    schema = create_empty_schema("Entwurf")
    with pytest.raises(ExecutionError):
        instantiate(schema)


def test_serial_run_completes() -> None:
    schema = _released_serial()
    instance = instantiate(schema)
    # exactly one activity ready at a time (A then B)
    ready = worklist(instance, schema)
    assert len(ready) == 1
    first = ready[0]
    instance = start_activity(instance, schema, first)
    assert instance.node_states[first] is NodeState.RUNNING
    instance = complete_activity(instance, schema, first)

    ready = worklist(instance, schema)
    assert len(ready) == 1
    second = ready[0]
    assert second != first
    instance = complete_activity(instance, schema, second)

    assert instance.state is InstanceState.COMPLETED
    assert all(
        st in (NodeState.COMPLETED, NodeState.SKIPPED)
        for st in instance.node_states.values()
    )


def test_parallel_branches_are_concurrently_ready() -> None:
    schema = create_empty_schema("Parallel")
    schema = parallel_insert(schema, ["L", "R"], after_node_id="start")
    schema = release(schema)
    instance = instantiate(schema)

    ready = worklist(instance, schema)
    assert len(ready) == 2  # both AND branches active at once

    for node_id in list(ready):
        instance = complete_activity(instance, schema, node_id)

    assert instance.state is InstanceState.COMPLETED
    assert all(
        st is NodeState.COMPLETED
        for nid, st in instance.node_states.items()
        if schema.nodes[nid].type is NodeType.ACTIVITY
    )


def test_xor_decision_skips_unchosen_branch() -> None:
    schema = _released_conditional()
    erfassen = _nid(schema, "Erfassen")
    chosen = _nid(schema, "Leitung")
    not_chosen = _nid(schema, "Team")

    instance = instantiate(schema)
    # the split is never "pending" -- it resolves from data, not a manual choice
    assert pending_decisions(instance, schema) == []
    assert worklist(instance, schema) == [erfassen]

    # 1500 >= 1001 -> the Leitung branch is taken automatically on completion
    instance = complete_activity(instance, schema, erfassen, {"betrag": 1500})

    assert worklist(instance, schema) == [chosen]
    assert instance.node_states[not_chosen] is NodeState.SKIPPED

    instance = complete_activity(instance, schema, chosen)
    assert instance.state is InstanceState.COMPLETED
    assert instance.node_states[chosen] is NodeState.COMPLETED
    assert instance.node_states[not_chosen] is NodeState.SKIPPED


def test_xor_decision_takes_other_branch_for_other_data() -> None:
    schema = _released_conditional()
    erfassen = _nid(schema, "Erfassen")
    team = _nid(schema, "Team")
    leitung = _nid(schema, "Leitung")

    instance = instantiate(schema)
    # 500 < 1001 -> the Team branch is taken automatically
    instance = complete_activity(instance, schema, erfassen, {"betrag": 500})

    assert worklist(instance, schema) == [team]
    assert instance.node_states[leitung] is NodeState.SKIPPED


def test_complete_with_data_stores_values() -> None:
    schema = _released_serial()
    instance = instantiate(schema)
    first = worklist(instance, schema)[0]
    instance = complete_activity(instance, schema, first, {"betrag": 1500})
    assert instance.data_values["betrag"] == 1500


def test_start_non_activated_activity_fails() -> None:
    schema = _released_serial()
    instance = instantiate(schema)
    not_ready = next(
        nid
        for nid, st in instance.node_states.items()
        if st is NodeState.NOT_ACTIVATED
        and schema.nodes[nid].type is NodeType.ACTIVITY
    )
    with pytest.raises(ExecutionError):
        start_activity(instance, schema, not_ready)


def test_missing_discriminator_value_raises() -> None:
    schema = _released_conditional()
    erfassen = _nid(schema, "Erfassen")
    instance = instantiate(schema)
    # completing the writing step without supplying the discriminator leaves the
    # split unable to resolve -> a runtime error (never a silent deadlock).
    instance = start_activity(instance, schema, erfassen)
    with pytest.raises(ExecutionError):
        complete_activity(instance, schema, erfassen)
