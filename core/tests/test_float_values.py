# SPDX-License-Identifier: BUSL-1.1
"""Runtime FLOAT values must be finite numbers (boundary check D3).

The API's JSON parser accepts the literals ``NaN`` and ``Infinity``. Every
comparison with NaN is false, so such a value slipped past a mask field's
min/max check and silently took the open last branch of a THRESHOLD decision.
The boundary type check now rejects non-finite FLOAT values on every inbound
HTTP path (data write, step completion, external tasks, single-instance
migration, simulation seed), with the same coded finding as any other wrong
type. Library callers that skip the boundary are caught by the engine: it
finds no THRESHOLD cell for a non-finite value and aborts instead of routing.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

from procworks import (
    AccessMode,
    BranchSpec,
    add_data_element,
    conditional_insert,
    connect_data,
    create_empty_schema,
    serial_insert,
)
from procworks.api import app
from procworks.model import (
    DataType,
    XorBranch,
    XorDecisionKind,
    matching_partition_cell,
    value_matches_type,
)
from procworks.simulation import simulate

client = TestClient(app)


@pytest.mark.parametrize(
    "value", [0, -3, 2.5, -0.0, 1e300, 10**400], ids=["0", "-3", "2.5", "-0.0", "1e300", "10**400"]
)
def test_finite_numbers_are_floats(value: object) -> None:
    """Ordinary numbers pass -- also an int too large to convert to float."""

    assert value_matches_type(DataType.FLOAT, value)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), True, "1.5", None],
    ids=["nan", "+inf", "-inf", "bool", "str", "none"],
)
def test_non_finite_or_non_numeric_values_are_not_floats(value: object) -> None:
    assert not value_matches_type(DataType.FLOAT, value)


def _raw(method: str, url: str, body: str) -> Any:
    """Send ``body`` as raw JSON text.

    The test client's ``json=`` encoder refuses NaN/Infinity; other clients
    (and Python's ``json.dumps``) send the literals.
    """

    return client.request(method, url, content=body, headers={"content-type": "application/json"})


def _decision_instance(name: str) -> tuple[str, str, dict[str, str]]:
    """Released process: Erfassen writes FLOAT 'menge' -> XOR [<10 Klein | Groß].

    Returns the instance id, the id of 'Erfassen' and a node id -> label map.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Menge", "data_type": "FLOAT", "element_id": "menge"},
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": act, "element_id": "menge", "mode": "WRITE", "mandatory": True},
    )
    split = client.post(
        f"/schemas/{sid}/conditional-insert",
        json={
            "after_node_id": act,
            "discriminator": "menge",
            "branches": [{"label": "Klein", "upper": 10}, {"label": "Groß"}],
        },
    )
    assert split.status_code == 200, split.text
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    labels = {
        n["id"]: n["label"] for n in client.get(f"/schemas/{sid}").json()["nodes"].values()
    }
    return iid, act, labels


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_completion_rejects_a_non_finite_decision_value(literal: str) -> None:
    """NaN used to route silently into the open 'Groß' branch; now 422, nothing stored."""

    iid, act, labels = _decision_instance(f"Menge {literal}")

    resp = _raw(
        "POST",
        f"/instances/{iid}/complete",
        f'{{"node_id": "{act}", "data": {{"menge": {literal}}}}}',
    )

    assert resp.status_code == 422
    assert {f["code"] for f in resp.json()["detail"]["findings"]} == {"D3.wrong-type"}
    instance = client.get(f"/instances/{iid}").json()
    assert "menge" not in instance["data_values"]
    # The step is still open; a finite value then decides as modelled.
    done = client.post(f"/instances/{iid}/complete", json={"node_id": act, "data": {"menge": 3.5}})
    assert done.status_code == 200, done.text
    ready = client.get(f"/instances/{iid}/worklist").json()["ready_activities"]
    assert [labels[r] for r in ready] == ["Klein"]


def test_data_write_rejects_a_non_finite_float() -> None:
    iid, _, _ = _decision_instance("Menge Daten")

    resp = _raw("PUT", f"/instances/{iid}/data", '{"values": {"menge": NaN}}')

    assert resp.status_code == 422
    assert {f["code"] for f in resp.json()["detail"]["findings"]} == {"D3.wrong-type"}
    assert client.get(f"/instances/{iid}").json()["data_values"] == {}


# --- the engine itself never routes a non-finite value ------------------------


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), float("-inf")], ids=["nan", "+inf", "-inf"]
)
def test_engine_finds_no_cell_for_a_non_finite_value(value: float) -> None:
    """Root cause, independent of any boundary: NaN compares false with every
    bound and used to land in the open last cell. Now no cell matches, and the
    caller raises its runtime error (``EX.no-branch``) instead of guessing."""

    cells = [XorBranch(target="klein", upper=10), XorBranch(target="gross")]
    assert matching_partition_cell(XorDecisionKind.THRESHOLD, cells, value) is None
    assert matching_partition_cell(XorDecisionKind.THRESHOLD, cells, 3.5) is cells[0]
    assert matching_partition_cell(XorDecisionKind.THRESHOLD, cells, 10**400) is cells[1]


@pytest.mark.parametrize(
    ("seed", "code"),
    [
        ('{"menge": NaN}', "D3.wrong-type"),
        ('{"menge": "viel"}', "D3.wrong-type"),
        ('{"gibt-es-nicht": 1}', "D3.unknown-element"),
    ],
    ids=["nan", "text", "unknown"],
)
def test_simulation_checks_its_seed_values_like_instance_data(seed: str, code: str) -> None:
    """The what-if run used to seed values unchecked, so a mistyped or NaN value
    silently took a branch and the result misled. Same boundary as real data."""

    sid = client.post("/schemas", json={"name": "Simulation NaN"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    setup = [
        client.post(
            f"/schemas/{sid}/data-elements",
            json={"name": "Menge", "data_type": "FLOAT", "element_id": "menge"},
        ),
        client.post(
            f"/schemas/{sid}/data-access",
            json={"node_id": act, "element_id": "menge", "mode": "WRITE"},
        ),
        client.post(
            f"/schemas/{sid}/conditional-insert",
            json={
                "after_node_id": act,
                "discriminator": "menge",
                "branches": [{"label": "Klein", "upper": 10}, {"label": "Groß"}],
            },
        ),
    ]
    assert [r.status_code for r in setup] == [200, 200, 200], [r.text for r in setup]

    resp = _raw("POST", f"/schemas/{sid}/simulate", f'{{"data": {seed}}}')

    assert resp.status_code == 422, resp.text
    assert [f["code"] for f in resp.json()["detail"]["findings"]] == [code]


def test_library_simulation_aborts_on_a_nan_seed_instead_of_taking_a_branch() -> None:
    """Library callers seed ``simulate`` directly, without the HTTP check. The
    engine stops at the decision with its reason instead of taking 'Groß'."""

    schema = create_empty_schema("Simulation Bibliothek")
    schema = serial_insert(schema, "Erfassen", after_node_id="start")
    act = next(n.id for n in schema.nodes.values() if n.label == "Erfassen")
    schema = add_data_element(schema, "Menge", DataType.FLOAT, element_id="menge")
    schema = connect_data(schema, act, "menge", AccessMode.WRITE)
    schema = conditional_insert(
        schema,
        after_node_id=act,
        discriminator="menge",
        branches=[BranchSpec(label="Klein", upper=10), BranchSpec(label="Groß")],
    )

    result = simulate(schema, {"menge": float("nan")})

    assert result.completed is False
    assert result.decisions == {}
    assert result.findings == [
        "Abbruch bei „Erfassen“: Für den Wert von „Menge“ passt kein Zweig der Entscheidung."
    ]


def test_simulation_still_runs_with_a_valid_seed() -> None:
    """Counterpart: a fitting value is accepted and decides as modelled."""

    sid = client.post("/schemas", json={"name": "Simulation gültig"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Menge", "data_type": "FLOAT", "element_id": "menge"},
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": act, "element_id": "menge", "mode": "WRITE"},
    )
    client.post(
        f"/schemas/{sid}/conditional-insert",
        json={
            "after_node_id": act,
            "discriminator": "menge",
            "branches": [{"label": "Klein", "upper": 10}, {"label": "Groß"}],
        },
    )

    resp = client.post(f"/schemas/{sid}/simulate", json={"data": {"menge": 3.5}})

    assert resp.status_code == 200, resp.text
    assert resp.json()["completed"] is True
