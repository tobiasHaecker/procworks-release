# SPDX-License-Identifier: BUSL-1.1
"""E6: side-effect-free what-if simulation (Simulations-Konzept).

The walk uses the pure engine on a throw-away instance: chosen XOR branches
and loop rounds follow the seeded values, a missing decision value aborts
with a clear finding, endless loops hit the cap, and the expected duration
is the longest target-duration path over the executed nodes -- parallel
branches as maximum, loop bodies multiplied by their simulated rounds.
Purity is asserted directly: a simulation leaves no instance behind.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from procworks import (
    AccessMode,
    BranchSpec,
    DataType,
    LoopCell,
    NodeState,
    add_data_element,
    conditional_insert,
    connect_data,
    create_empty_schema,
    insert_loop,
    parallel_insert,
    serial_insert,
    set_time_constraint,
)
from procworks import api as api_module
from procworks.model import TimeConstraint
from procworks.simulation import simulate

client = TestClient(api_module.app)


def _nid(schema: object, label: str) -> str:
    return next(n.id for n in schema.nodes.values() if n.label == label)  # type: ignore[attr-defined]


def _xor_schema():
    """Erfassen writes 'eilig'; XOR branches Schnell (true) / Gruendlich."""

    schema = create_empty_schema("Sim")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = add_data_element(schema, "eilig", DataType.BOOLEAN, element_id="eilig")
    schema = connect_data(schema, _nid(schema, "Erfassen"), "eilig", AccessMode.WRITE)
    return conditional_insert(
        schema,
        after_node_id=_nid(schema, "Erfassen"),
        discriminator="eilig",
        branches=[
            BranchSpec(label="Schnell", bool_value=True),
            BranchSpec(label="Gruendlich", bool_value=False),
        ],
    )


def test_simulation_takes_the_branch_the_values_select() -> None:
    schema = _xor_schema()
    fast = simulate(schema, {"eilig": True})
    assert fast.completed
    assert fast.node_states[_nid(schema, "Schnell")] is NodeState.COMPLETED
    assert fast.node_states[_nid(schema, "Gruendlich")] is NodeState.SKIPPED
    assert list(fast.decisions.values()) == [_nid(schema, "Schnell")]

    slow = simulate(schema, {"eilig": False})
    assert slow.node_states[_nid(schema, "Gruendlich")] is NodeState.COMPLETED
    assert slow.node_states[_nid(schema, "Schnell")] is NodeState.SKIPPED


def test_simulation_reports_a_missing_decision_value() -> None:
    result = simulate(_xor_schema(), {})
    assert not result.completed
    assert any("Abbruch" in f for f in result.findings)


def test_simulation_runs_loops_and_caps_endless_repetition() -> None:
    schema = create_empty_schema("Schleife")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = add_data_element(schema, "disc", DataType.INTEGER, element_id="disc")
    schema = insert_loop(
        schema,
        _nid(schema, "Erfassen"),
        "Pruefen",
        discriminator="disc",
        cells=[LoopCell(repeat=False, upper=1), LoopCell(repeat=True)],
    )
    le = next(nid for nid, n in schema.nodes.items() if n.type.value == "LOOP_END")

    exits = simulate(schema, {"disc": 0})
    assert exits.completed and exits.loop_iterations.get(le, 0) == 0

    endless = simulate(schema, {"disc": 5}, loop_cap=7)
    assert not endless.completed
    assert endless.loop_iterations[le] >= 7
    assert any("Schleifen-Abbruch" in f for f in endless.findings)


def test_simulation_duration_uses_maximum_of_parallel_branches() -> None:
    schema = create_empty_schema("Parallel")
    schema = parallel_insert(schema, ["Links", "Rechts"], after_node_id="start")
    schema = set_time_constraint(
        schema, _nid(schema, "Links"), TimeConstraint(max_duration_seconds=100)
    )
    schema = set_time_constraint(
        schema, _nid(schema, "Rechts"), TimeConstraint(max_duration_seconds=40)
    )
    result = simulate(schema)
    assert result.completed
    assert result.expected_duration_seconds == 100  # max, not sum


def test_simulation_duration_multiplies_simulated_loop_rounds() -> None:
    schema = create_empty_schema("Runden")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    schema = add_data_element(schema, "disc", DataType.INTEGER, element_id="disc")
    schema = insert_loop(
        schema,
        _nid(schema, "Erfassen"),
        "Pruefen",
        discriminator="disc",
        cells=[LoopCell(repeat=False, upper=1), LoopCell(repeat=True)],
        max_iterations=3,  # the engine brake ends the endless seed after 3 runs
    )
    schema = set_time_constraint(
        schema, _nid(schema, "Pruefen"), TimeConstraint(max_duration_seconds=10)
    )
    result = simulate(schema, {"disc": 5})
    assert result.completed  # the modelled brake, not the simulation cap
    assert result.expected_duration_seconds == 30  # 3 simulated rounds x 10s
    assert not result.findings


def test_simulation_is_pure_no_instance_is_left_behind() -> None:
    sid = client.post("/schemas", json={"name": "Sim-API"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "Erfassen", "after_node_id": "start"},
    )
    before = client.get("/instances").json()

    resp = client.post(f"/schemas/{sid}/simulate", json={"data": {}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["completed"] is True
    assert body["executed"], "der Arbeitsschritt wurde durchgespielt"

    assert client.get("/instances").json() == before  # nichts angelegt
