# SPDX-License-Identifier: BUSL-1.1
"""A restarted API never hands out an id that is already stored (Nachtest 2026-09-27, NT-01).

The ids ``instance_<n>``, ``schema_<n>``, ``tpl_<n>``, ``org_<n>`` (and the
node/element ids inside a schema) came from process-local counters starting
at 1. After ``docker compose restart api`` the first new instance became
``instance_1`` again and -- the stores save by upsert -- silently *replaced*
the stored ``instance_1``; a new schema replaced ``schema_1``.

A restart is simulated here by setting the sequences back to 1
(:meth:`IdSequence.reset_for_tests`), exactly what a fresh process sees.
Two independent measures are pinned, each on its own:

* the **guards** (store lookup before an id is handed out) -- tested with the
  counters reset and *no* start-up lift;
* the **start-up lift** (``_reserve_stored_ids`` in ``_lifespan``) -- tested
  without relying on the guards.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

import procworks.api as api_module
from procworks import ids
from procworks.api import app

client = TestClient(app)


@pytest.fixture
def restart() -> Iterator[None]:
    """Let a test set the sequences back; restore a sane floor afterwards.

    The body may call :func:`_simulate_restart`. Afterwards the sequences are
    lifted past everything stored again, so later tests of the suite keep
    getting fresh ids.
    """

    yield
    api_module._reserve_stored_ids()


def _simulate_restart(at: int = 1) -> None:
    """Put every id sequence back to ``at`` -- the state of a freshly started process.

    A real restart goes back to 1. In this long test process the objects of a
    single test carry high numbers, so a test that needs the counter to land
    *exactly* on stored ids passes the lowest of them as ``at``.
    """

    for sequence in (ids.INSTANCE_IDS, ids.MODEL_IDS, ids.ORG_IDS):
        sequence.reset_for_tests(at)


def _number(key: str) -> int:
    """The ``<n>`` of a generated id ``<prefix>_<n>``."""

    return int(key.rsplit("_", 1)[1])


def _released_schema(name: str) -> str:
    """A released one-step schema, built in open mode; returns its id."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    step = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    client.post(f"/schemas/{sid}/roles", json={"name": "sb", "role_id": "sb"})
    client.post(f"/schemas/{sid}/agents", json={"name": "a1", "role_ids": ["sb"], "agent_id": "a1"})
    rule = {"kind": "ROLE", "ref": "sb"}
    client.post(f"/schemas/{sid}/staff-rule", json={"node_id": step, "rule": rule})
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid


# ---------------------------------------------------------------------------
# Guards: correct even when the counters restart at 1 and nothing lifts them.
# ---------------------------------------------------------------------------


def test_new_instance_after_restart_never_replaces_a_stored_one(restart: None) -> None:
    sid = _released_schema("NT-01 Instanz")
    first = client.post(f"/schemas/{sid}/instances").json()["id"]
    before = client.get(f"/instances/{first}").json()

    _simulate_restart(at=_number(first))
    created = [client.post(f"/schemas/{sid}/instances").json()["id"] for _ in range(3)]

    assert first not in created
    assert len(set(created)) == 3
    assert client.get(f"/instances/{first}").json() == before


def test_new_schema_after_restart_never_replaces_a_stored_one(restart: None) -> None:
    existing = client.post("/schemas", json={"name": "NT-01 bestehend"}).json()["id"]

    _simulate_restart(at=_number(existing))
    new_ids = [
        client.post("/schemas", json={"name": f"NT-01 neu {i}"}).json()["id"] for i in range(3)
    ]

    assert existing not in new_ids
    assert client.get(f"/schemas/{existing}").json()["name"] == "NT-01 bestehend"


def test_revision_after_restart_never_replaces_a_stored_schema(restart: None) -> None:
    sid = _released_schema("NT-01 Revision")
    other = client.post("/schemas", json={"name": "NT-01 Nachbar"}).json()["id"]

    _simulate_restart(at=_number(other))
    for _ in range(3):
        revision = client.post(f"/schemas/{sid}/revision", json={}).json()["id"]
        assert revision not in {sid, other}
    assert client.get(f"/schemas/{other}").json()["name"] == "NT-01 Nachbar"


def test_new_org_model_and_template_after_restart_never_replace_stored_ones(
    restart: None,
) -> None:
    org = client.post("/org-models", json={"name": "NT-01 Org"}).json()["id"]
    sid = _released_schema("NT-01 Vorlage")
    template = client.post("/templates", json={"schema_id": sid, "name": "NT-01 T"}).json()["id"]

    _simulate_restart(at=_number(org))
    orgs = [client.post("/org-models", json={"name": f"Org {i}"}).json()["id"] for i in range(3)]
    _simulate_restart(at=_number(template))
    templates = [
        client.post("/templates", json={"schema_id": sid, "name": f"T {i}"}).json()["id"]
        for i in range(3)
    ]

    assert org not in orgs
    assert template not in templates
    assert client.get(f"/org-models/{org}").json()["name"] == "NT-01 Org"
    assert client.get(f"/templates/{template}").json()["name"] == "NT-01 T"


# ---------------------------------------------------------------------------
# Start-up lift: the sequences continue behind the persisted state.
# ---------------------------------------------------------------------------


def test_startup_lifts_every_sequence_past_the_stored_ids(restart: None) -> None:
    sid = _released_schema("NT-01 Start")
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    schema = client.get(f"/schemas/{sid}").json()
    highest_node = max(
        int(key.rsplit("_", 1)[1]) for key in schema["nodes"] if key.rsplit("_", 1)[-1].isdigit()
    )

    _simulate_restart()
    with TestClient(app):  # runs _lifespan, i.e. the real start-up path
        pass

    floor = max(int(iid.rsplit("_", 1)[1]), highest_node, int(sid.rsplit("_", 1)[1]))
    for sequence in (ids.INSTANCE_IDS, ids.MODEL_IDS, ids.ORG_IDS):
        assert sequence.peek() > floor


def test_node_ids_inside_an_existing_schema_do_not_clash_after_restart(restart: None) -> None:
    """Without the lift the first inserts got ids the schema already used (K2)."""

    sid = client.post("/schemas", json={"name": "NT-01 Knoten"}).json()["id"]
    for label in ("Eins", "Zwei", "Drei"):
        response = client.post(
            f"/schemas/{sid}/serial-insert", json={"label": label, "after_node_id": "start"}
        )
        assert response.status_code == 200
    lowest = min(int(key.rsplit("_", 1)[1]) for key in response.json()["nodes"] if "_" in key)

    _simulate_restart(at=lowest)  # the counter would now re-issue this schema's node ids
    api_module._reserve_stored_ids()
    for label in ("Vier", "Fuenf", "Sechs"):
        response = client.post(
            f"/schemas/{sid}/serial-insert", json={"label": label, "after_node_id": "start"}
        )
        assert response.status_code == 200, response.json()
    labels = {n["label"] for n in response.json()["nodes"].values()}
    assert {"Eins", "Zwei", "Drei", "Vier", "Fuenf", "Sechs"} <= labels


# ---------------------------------------------------------------------------
# The allocator itself.
# ---------------------------------------------------------------------------


def test_guard_skips_taken_ids_and_the_counter_never_goes_back() -> None:
    sequence = ids.IdSequence()
    taken = {"x_1", "x_2", "x_4"}
    sequence.guard("x", lambda key: key in taken)

    assert [sequence.new("x") for _ in range(3)] == ["x_3", "x_5", "x_6"]
    assert sequence.new("y") == "y_7"  # other prefix: no guard, same counter
    sequence.advance_past(["x_2"])  # lower than the counter: no effect
    assert sequence.peek() == 8


def test_advance_past_counts_only_generated_looking_ids() -> None:
    assert ids.max_generated_number(["act_12", "loopend_7", "field_3"]) == 12
    assert ids.max_generated_number(["urlaub-2026-001", "B-1234", "Antrag 99", "x_"]) == 0
    assert ids.max_generated_number(["Act_5", "act_5a", "5"]) == 0


def test_reserve_scans_every_string_of_a_document() -> None:
    sequence_floor = ids.reserve_existing_ids(
        instance_ids=["instance_4", "o2c-2026-001"],
        documents=[{"nodes": {"act_40": {"id": "act_40"}}, "fields": [{"id": "field_9"}]}],
    )
    assert sequence_floor == 40
    assert ids.MODEL_IDS.peek() > 40
    assert ids.INSTANCE_IDS.peek() > 40


def test_concurrent_allocation_hands_out_each_id_once() -> None:
    sequence = ids.IdSequence()
    results: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        mine = [sequence.new("instance") for _ in range(200)]
        with lock:
            results.extend(mine)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == len(set(results)) == 1600
