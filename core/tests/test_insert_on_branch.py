# SPDX-License-Identifier: BUSL-1.1
"""Einfügen am Anfang eines Zweigs -- auch in einen leeren XOR-Zweig.

``serial_insert`` fügte nur hinter Knoten mit genau einem Ausgang ein; das
„+“ auf einer Kante aus einer Verzweigung endete deshalb mit „mehrere
Nachfolger“, und ein leerer Zweig ließ sich gar nicht wieder füllen.
``before_node_id`` wählt die Kante ``split -> ziel``: weiterhin ein serieller
Splice auf *einer* Kante, die Zweigbedingung wandert mit.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from staffing import staffed
from test_empty_branch import _join, _nid, _split, _threshold_schema

from procworks import complete_activity, delete_node, instantiate, release, serial_insert, validate
from procworks.api import app
from procworks.model import NodeState
from procworks.operations import CorrectnessError


def test_insert_into_an_empty_branch_restores_it_with_its_condition() -> None:
    schema = _threshold_schema()
    split, join = _split(schema), _join(schema)
    schema = delete_node(schema, _nid(schema, "Team"))  # Team-Zweig ist jetzt leer

    schema = serial_insert(schema, "Kurzprüfung", after_node_id=split, before_node_id=join)

    new = _nid(schema, "Kurzprüfung")
    [first] = [e for e in schema.edges if e.source == split and e.target == new]
    assert first.condition == "betrag < 1001"  # Bedingung wandert mit
    assert not [e for e in schema.edges if e.source == split and e.target == join]
    assert {b.target for b in schema.xor_decisions[split].branches} == {
        new, _nid(schema, "Leitung")}
    assert validate(schema) == []


def test_insert_at_the_start_of_a_filled_branch() -> None:
    schema = _threshold_schema()
    split, leitung = _split(schema), _nid(schema, "Leitung")

    schema = serial_insert(schema, "Vorprüfung", after_node_id=split, before_node_id=leitung)

    vor = _nid(schema, "Vorprüfung")
    assert any(e.source == vor and e.target == leitung for e in schema.edges)
    [first] = [e for e in schema.edges if e.source == split and e.target == vor]
    assert first.condition == "betrag >= 1001"
    assert validate(schema) == []


def test_engine_runs_the_step_inserted_into_the_formerly_empty_branch() -> None:
    schema = _threshold_schema()
    split, join = _split(schema), _join(schema)
    schema = delete_node(schema, _nid(schema, "Team"))
    schema = serial_insert(schema, "Kurzprüfung", after_node_id=split, before_node_id=join)
    released = release(staffed(schema))
    erfassen, kurz = _nid(released, "Erfassen"), _nid(released, "Kurzprüfung")

    inst = complete_activity(instantiate(released), released, erfassen, {"betrag": 10})

    assert inst.node_states[kurz] is NodeState.ACTIVATED


def test_multi_exit_anchor_without_target_is_still_refused() -> None:
    """Negativfall: ohne before_node_id bleibt die alte Ablehnung -- mit Grund."""
    schema = _threshold_schema()
    with pytest.raises(CorrectnessError) as exc:
        serial_insert(schema, "X", after_node_id=_split(schema))
    assert [f.code for f in exc.value.findings] == ["OP.anchor-not-serial"]


def test_unknown_edge_is_refused_naming_both_steps() -> None:
    schema = _threshold_schema()
    with pytest.raises(CorrectnessError) as exc:
        serial_insert(schema, "X", after_node_id=_split(schema),
                      before_node_id=_nid(schema, "Erfassen"))
    [finding] = exc.value.findings
    assert finding.code == "OP.no-edge"
    assert finding.params["target"] == "Erfassen"


def test_api_accepts_before_node_id() -> None:
    client = TestClient(app)
    sid = client.post("/schemas", json={"name": "Zweiganfang"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    schema = client.get(f"/schemas/{sid}").json()
    a = next(n["id"] for n in schema["nodes"].values() if n["label"] == "A")
    client.post(f"/schemas/{sid}/parallel-insert",
                json={"branch_labels": ["P", "Q"], "after_node_id": a})
    schema = client.get(f"/schemas/{sid}").json()
    split = next(n["id"] for n in schema["nodes"].values() if n["type"] == "AND_SPLIT")
    q = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Q")
    resp = client.post(f"/schemas/{sid}/serial-insert",
                       json={"label": "Vor Q", "after_node_id": split, "before_node_id": q})
    assert resp.status_code == 200, resp.text
    labels = {n["label"] for n in resp.json()["nodes"].values()}
    assert "Vor Q" in labels


def test_branch_keeps_its_place_among_the_split_exits() -> None:
    """Die Reihenfolge der Ausgänge ist die Reihenfolge der Zweige im Bild --
    ein befüllter Zweig springt nicht nach unten."""
    schema = _threshold_schema()
    split, team = _split(schema), _nid(schema, "Team")
    before = [e.target for e in schema.outgoing(split)]
    position = before.index(team)
    assert position < len(before) - 1  # sonst prüfte der Test nichts

    schema = serial_insert(schema, "Vorprüfung", after_node_id=split, before_node_id=team)

    after = [e.target for e in schema.outgoing(split)]
    assert after[position] == _nid(schema, "Vorprüfung")
    assert len(after) == len(before)
