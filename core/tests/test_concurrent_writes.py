# SPDX-License-Identifier: BUSL-1.1
"""Concurrent writes never lose a confirmed change (conditional save).

Every API action loads a case or a model, changes it and saves it. Without a
check, two requests that load the same state both save, and the later save
silently replaces the earlier one: both callers get 200, one change is gone,
and the audit shows the same step claimed or completed twice. A double click
or two eligible colleagues are enough.

The fix is an optimistic version check: every stored case and model carries a
version, a save only succeeds if the version is still the one that was read
(``ConcurrentModification`` otherwise), and every mutating request runs as one
unit of work that the server repeats on such a conflict -- the repetition
reads the new state and gives the answer that state deserves (already
claimed, not ready, not a draft), or simply succeeds when the two changes do
not collide (two branches of one case, two inserts into one model). Only when
the conflict persists does the caller get ``409 DB.conflict``.

Pinned here, against the in-memory stores and against SQLAlchemy stores on an
SQLite file:

* the store contract itself -- a stale save is refused with the reason, the
  first save survives, a loaded copy is detached from the store;
* the races of the request paths, forced with a barrier after the load: claim,
  completion with different data, completion of two parallel branches, two
  inserts into one model, two releases, two starts with one Idempotency-Key;
* the answer when the conflict does not go away (code, parameters, nothing
  saved);
* the escalation sweep, which runs on every worklist read, skips a case that
  changed under it instead of saving over the change.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from sqlalchemy.exc import DBAPIError, OperationalError
from staffing import staff_via_api
from web_vm import needs_node, run_app_js

import procworks.api as api_module
from procworks import db, unit_of_work
from procworks import execution as exe
from procworks import operations as ops
from procworks.api import app
from procworks.audit import EventType
from procworks.model import ProcessInstance
from procworks.store import InMemoryInstanceStore, InMemorySchemaStore
from procworks.unit_of_work import ConcurrentModification

client = TestClient(app)

#: Seconds the barrier waits for the second request. With in-memory stores the
#: writes of the API are serialised, so the second request can never reach the
#: barrier while the first holds it -- the wait ends after this timeout.
_BARRIER_TIMEOUT = 0.5


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Run the test once on the default in-memory stores and once on SQLite."""

    if request.param == "sqlite":
        use_sql_backend(monkeypatch, tmp_path)
    return str(request.param)


def _gate(monkeypatch: pytest.MonkeyPatch, module: object, name: str) -> None:
    """Hold the first two calls of ``module.name`` until both have arrived.

    Placed between the load and the save of a request, so two concurrent
    requests both work on the state they read before either of them saves --
    the interleaving that lost an update. Later calls (the server's own
    repetition after a conflict) pass straight through.
    """

    original = getattr(module, name)
    barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)
    counter = itertools.count()
    lock = threading.Lock()

    def gated(*args: Any, **kwargs: Any) -> Any:
        with lock:
            call = next(counter)
        if call < 2:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, gated)


def _race(*calls: Callable[[], httpx.Response]) -> list[httpx.Response]:
    """Send the requests at the same time and return their answers in order."""

    with ThreadPoolExecutor(len(calls)) as pool:
        futures = [pool.submit(call) for call in calls]
        return [future.result() for future in futures]


def _two_agent_schema(name: str) -> tuple[str, str]:
    """start -> "Erfassen" (writes "tage") -> "Prüfen" -> end.

    "Erfassen" is offered to role "sb", held by a1 and a2. The second step
    keeps the case running after the first, so a late completion meets a
    step that is no longer ready (rather than a finished case).
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "Prüfen", "after_node_id": act})
    client.post(f"/schemas/{sid}/roles", json={"name": "Sachbearbeitung", "role_id": "sb"})
    for agent in ("a1", "a2"):
        client.post(
            f"/schemas/{sid}/agents",
            json={"name": agent, "role_ids": ["sb"], "agent_id": agent},
        )
    client.post(
        f"/schemas/{sid}/staff-rule", json={"node_id": act, "rule": {"kind": "ROLE", "ref": "sb"}}
    )
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "Tage", "data_type": "INTEGER", "element_id": "tage"},
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": act, "element_id": "tage", "mode": "WRITE", "mandatory": True},
    )
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid, act


def _start(sid: str) -> str:
    resp = client.post(f"/schemas/{sid}/instances")
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _events(iid: str, event_type: EventType) -> list[Any]:
    return [e for e in api_module._audit.for_instance(iid) if e.event_type is event_type]


def _codes(resp: httpx.Response) -> set[str]:
    detail = resp.json()["detail"]
    if "findings" in detail:
        return {f["code"] for f in detail["findings"]}
    return {detail["code"]}


# --- the store contract --------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def stores(
    request: pytest.FixtureRequest, tmp_path: Path
) -> tuple[Any, Any]:
    """An instance and a schema store, in memory or on an SQLite file."""

    if request.param == "memory":
        return InMemoryInstanceStore(), InMemorySchemaStore()
    url = f"sqlite:///{tmp_path / 'stores.db'}"
    return (
        db.SqlAlchemyInstanceStore(url, create_tables=True),
        db.SqlAlchemySchemaStore(url, create_tables=True),
    )


def _instance(instance_id: str = "instance_1") -> ProcessInstance:
    return ProcessInstance(id=instance_id, schema_id="schema_1")


def test_a_stale_instance_save_is_refused_and_the_first_save_survives(
    stores: tuple[Any, Any],
) -> None:
    instances, _ = stores
    instances.put(_instance())
    first = instances.get("instance_1")
    second = instances.get("instance_1")

    first.data_values["tage"] = 2
    instances.put(first)
    second.data_values["tage"] = 9
    with pytest.raises(ConcurrentModification) as refused:
        instances.put(second)

    assert (refused.value.kind, refused.value.key) == ("instance", "instance_1")
    assert instances.get("instance_1").data_values == {"tage": 2}


def test_a_stale_schema_save_is_refused_and_the_first_save_survives(
    stores: tuple[Any, Any],
) -> None:
    _, schemas = stores
    schemas.put(ops.create_empty_schema("Urlaub"))
    sid = schemas.list_ids()[0]
    first = schemas.get(sid)
    second = schemas.get(sid)

    schemas.put(ops.serial_insert(first, "Erfassen", "start"))
    with pytest.raises(ConcurrentModification) as refused:
        schemas.put(ops.serial_insert(second, "Prüfen", "start"))

    assert (refused.value.kind, refused.value.key) == ("schema", sid)
    labels = {node.label for node in schemas.get(sid).nodes.values()}
    assert "Erfassen" in labels and "Prüfen" not in labels


def test_saving_again_after_an_own_save_is_no_conflict(stores: tuple[Any, Any]) -> None:
    """A request stores the same case several times (engine, stamps, audit hand-over)."""

    instances, _ = stores
    instances.put(_instance())
    loaded = instances.get("instance_1")
    instances.put(loaded)
    loaded.data_values["tage"] = 3
    instances.put(loaded)
    copy = loaded.model_copy(deep=True)  # the engine works on deep copies
    copy.data_values["tage"] = 4
    instances.put(copy)

    assert instances.get("instance_1").data_values == {"tage": 4}


def test_a_new_object_never_replaces_a_stored_one(stores: tuple[Any, Any]) -> None:
    """Creating is not updating: a second object under a stored id is a conflict.

    Two requests that create under the same id (an import with an explicit id,
    a revision with an explicit id) used to overwrite each other.
    """

    instances, _ = stores
    instances.put(_instance())
    with pytest.raises(ConcurrentModification) as refused:
        instances.put(_instance())
    assert refused.value.key == "instance_1"


def test_a_loaded_copy_is_detached_from_the_store(stores: tuple[Any, Any]) -> None:
    """Only a save changes the store -- in memory as on a database."""

    instances, _ = stores
    instances.put(_instance())
    loaded = instances.get("instance_1")
    loaded.data_values["tage"] = 5

    assert instances.get("instance_1").data_values == {}


def test_of_two_threads_saving_the_same_version_exactly_one_wins(
    stores: tuple[Any, Any],
) -> None:
    instances, _ = stores
    instances.put(_instance())
    barrier = threading.Barrier(2, timeout=5)
    outcomes: list[str] = []

    def save(value: int) -> None:
        loaded = instances.get("instance_1")
        barrier.wait()
        loaded.data_values["tage"] = value
        try:
            instances.put(loaded)
            outcomes.append("saved")
        except ConcurrentModification:
            outcomes.append("conflict")

    threads = [threading.Thread(target=save, args=(v,)) for v in (2, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["conflict", "saved"]


# --- the request paths ---------------------------------------------------------


def test_two_concurrent_claims_give_one_owner_and_one_claim_event(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid, act = _two_agent_schema(f"Gleichzeitig übernehmen ({backend})")
    iid = _start(sid)
    _gate(monkeypatch, exe, "claim_activity")

    answers = _race(
        lambda: client.post(f"/instances/{iid}/claim", json={"node_id": act, "agent_id": "a1"}),
        lambda: client.post(f"/instances/{iid}/claim", json={"node_id": act, "agent_id": "a2"}),
    )

    assert sorted(a.status_code for a in answers) == [200, 409], [a.text for a in answers]
    winner = next(a for a in answers if a.status_code == 200).json()["claimed_by"][act]
    loser = next(a for a in answers if a.status_code == 409)
    assert _codes(loser) == {"EX.claimed-by-other"}
    assert client.get(f"/instances/{iid}").json()["claimed_by"][act] == winner
    assert len(_events(iid, EventType.ACTIVITY_CLAIMED)) == 1


def test_two_concurrent_completions_keep_the_data_of_the_one_that_succeeded(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid, act = _two_agent_schema(f"Gleichzeitig abschließen ({backend})")
    iid = _start(sid)
    _gate(monkeypatch, exe, "complete_activity")

    def complete(agent: str, days: int) -> Callable[[], httpx.Response]:
        return lambda: client.post(
            f"/instances/{iid}/complete",
            json={"node_id": act, "agent_id": agent, "data": {"tage": days}},
        )

    answers = _race(complete("a1", 2), complete("a2", 9))

    assert sorted(a.status_code for a in answers) == [200, 409], [a.text for a in answers]
    winner = next(a for a in answers if a.status_code == 200).json()
    loser = next(a for a in answers if a.status_code == 409)
    assert _codes(loser) == {"EX.not-ready"}
    stored = client.get(f"/instances/{iid}").json()
    assert stored["data_values"]["tage"] == winner["data_values"]["tage"]
    assert len(_events(iid, EventType.ACTIVITY_COMPLETED)) == 1


def test_completions_of_two_parallel_branches_both_succeed(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Different steps of one case do not collide -- no change lost, no needless 409."""

    sid = client.post("/schemas", json={"name": f"Zwei Zweige ({backend})"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/parallel-insert",
        json={"branch_labels": ["Fachprüfung", "Budgetprüfung"], "after_node_id": "start"},
    ).json()
    steps = sorted(n["id"] for n in schema["nodes"].values() if n["type"] == "ACTIVITY")
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = _start(sid)
    _gate(monkeypatch, exe, "complete_activity")

    answers = _race(
        *[
            (lambda step=step: client.post(f"/instances/{iid}/complete", json={"node_id": step}))
            for step in steps
        ]
    )

    assert [a.status_code for a in answers] == [200, 200], [a.text for a in answers]
    stored = client.get(f"/instances/{iid}").json()
    assert stored["state"] == "COMPLETED"
    assert all(stored["node_states"][step] == "COMPLETED" for step in steps)
    assert len(_events(iid, EventType.ACTIVITY_COMPLETED)) == 2


def test_two_concurrent_inserts_into_one_model_both_land(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = client.post("/schemas", json={"name": f"Zwei Einfügungen ({backend})"}).json()["id"]
    _gate(monkeypatch, ops, "serial_insert")

    answers = _race(
        lambda: client.post(
            f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
        ),
        lambda: client.post(
            f"/schemas/{sid}/serial-insert", json={"label": "Prüfen", "after_node_id": "start"}
        ),
    )

    assert [a.status_code for a in answers] == [200, 200], [a.text for a in answers]
    labels = {n["label"] for n in client.get(f"/schemas/{sid}").json()["nodes"].values()}
    assert {"Erfassen", "Prüfen"} <= labels


def test_two_concurrent_releases_release_once(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = client.post("/schemas", json={"name": f"Zweimal freigeben ({backend})"}).json()["id"]
    client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    )
    staff_via_api(client, sid)
    _gate(monkeypatch, ops, "release")

    answers = _race(
        lambda: client.post(f"/schemas/{sid}/release"),
        lambda: client.post(f"/schemas/{sid}/release"),
    )

    assert sorted(a.status_code for a in answers) == [200, 422], [a.text for a in answers]
    assert _codes(next(a for a in answers if a.status_code == 422)) == {"LC.not-draft"}


def test_two_concurrent_revisions_open_one_draft(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most one open revision per version -- also when two ask at once."""

    sid, _ = _two_agent_schema(f"Zweimal überarbeiten ({backend})")
    _gate(monkeypatch, ops, "new_revision")

    answers = _race(
        lambda: client.post(f"/schemas/{sid}/revision", json={}),
        lambda: client.post(f"/schemas/{sid}/revision", json={}),
    )

    assert sorted(a.status_code for a in answers) == [200, 422], [a.text for a in answers]
    assert _codes(next(a for a in answers if a.status_code == 422)) == {"LC.open-revision"}
    drafts = [
        s
        for i in api_module._store.list_ids()
        if (s := api_module._store.get(i)) is not None and s.revision_of == sid
    ]
    assert len(drafts) == 1


def test_an_org_change_and_a_schema_change_cannot_break_staffing_together(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each change is valid against the other's old state; together they are not.

    The org change takes the last bearer away from role ``sb``, the model
    change assigns ``sb`` to a step. Checked one after the other, the second
    is refused (Z2: nobody may do the step). At the same time, each used to be
    checked against the state before the other -- both were stored, and the
    step had nobody.
    """

    org_id = f"org-race-{backend}"
    created = client.post("/org-models", json={"name": "Amt", "org_model_id": org_id})
    assert created.status_code == 201
    client.post(f"/org-models/{org_id}/roles", json={"name": "SB", "role_id": "sb"})
    client.post(f"/org-models/{org_id}/roles", json={"name": "LT", "role_id": "lt"})
    for name, role, agent in (("Erika", "sb", "a1"), ("Lena", "lt", "a2")):
        client.post(
            f"/org-models/{org_id}/agents",
            json={"name": name, "role_ids": [role], "agent_id": agent},
        )
    sid = client.post("/schemas", json={"name": f"Org im Wettlauf ({backend})"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    step = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    linked = client.post(f"/schemas/{sid}/org-model", json={"org_model_id": org_id})
    assert linked.status_code == 200

    # Both requests have read what they check against before either saves.
    barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT)
    passes = itertools.count()

    def wait_once() -> None:
        if next(passes) < 2:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass

    referencing = api_module._schemas_referencing
    assign = ops.assign_staff_rule

    def referencing_then_wait(org: str) -> Any:
        found = referencing(org)
        wait_once()
        return found

    def wait_then_assign(*args: Any, **kwargs: Any) -> Any:
        wait_once()
        return assign(*args, **kwargs)

    monkeypatch.setattr(api_module, "_schemas_referencing", referencing_then_wait)
    monkeypatch.setattr(ops, "assign_staff_rule", wait_then_assign)

    answers = _race(
        lambda: client.patch(f"/org-models/{org_id}/agents/a1", json={"role_ids": []}),
        lambda: client.post(
            f"/schemas/{sid}/staff-rule",
            json={"node_id": step, "rule": {"kind": "ROLE", "ref": "sb"}},
        ),
    )

    assert sorted(a.status_code for a in answers) == [200, 422], [a.text for a in answers]
    codes = {f["code"] for f in client.get(f"/schemas/{sid}/validation").json()["findings"]}
    assert "Z2.nobody" not in codes


def test_two_concurrent_starts_with_one_idempotency_key_start_one_instance(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid, _ = _two_agent_schema(f"Ein Schlüssel ({backend})")
    _gate(monkeypatch, exe, "instantiate")

    def start() -> httpx.Response:
        return client.post(
            f"/v1/schemas/{sid}/instances", headers={"Idempotency-Key": f"start-{backend}"}
        )

    answers = _race(start, start)

    assert [a.status_code for a in answers] == [201, 201], [a.text for a in answers]
    assert answers[0].json()["id"] == answers[1].json()["id"]
    started = [
        i
        for i in api_module._instances.list_ids()
        if (inst := api_module._instances.get(i)) is not None and inst.schema_id == sid
    ]
    assert len(started) == 1


def test_a_conflict_that_does_not_go_away_answers_409_and_saves_nothing(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer changes the case before every attempt: the caller hears why."""

    sid, act = _two_agent_schema(f"Dauerkonflikt ({backend})")
    iid = _start(sid)
    original = exe.claim_activity

    def claim_while_someone_else_saves(*args: Any, **kwargs: Any) -> ProcessInstance:
        def other_writer() -> None:
            stored = api_module._instances.get(iid)
            assert stored is not None
            stored.data_values["tage"] = 7
            api_module._instances.put(stored)

        writer = threading.Thread(target=other_writer)  # outside this request
        writer.start()
        writer.join()
        return original(*args, **kwargs)

    monkeypatch.setattr(exe, "claim_activity", claim_while_someone_else_saves)

    resp = client.post(f"/instances/{iid}/claim", json={"node_id": act, "agent_id": "a1"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "DB.conflict"
    assert resp.json()["detail"]["params"] == {"kind": "instance", "id": iid}
    stored = client.get(f"/instances/{iid}").json()
    assert stored["claimed_by"] == {}
    assert stored["data_values"] == {"tage": 7}  # the other writer's change survives
    assert _events(iid, EventType.ACTIVITY_CLAIMED) == []


def test_sequential_actions_keep_their_answers(backend: str) -> None:
    """No concurrency, no change: the second completion is refused as before."""

    sid, act = _two_agent_schema(f"Nacheinander ({backend})")
    iid = _start(sid)
    body = {"node_id": act, "agent_id": "a1", "data": {"tage": 2}}

    assert client.post(f"/instances/{iid}/complete", json=body).status_code == 200
    again = client.post(f"/instances/{iid}/complete", json=body)

    assert again.status_code == 409
    assert _codes(again) == {"EX.not-ready"}


@needs_node
@pytest.mark.parametrize(
    ("kind", "subject"), [("instance", "Der Vorgang"), ("schema", "Das Modell")]
)
def test_the_client_says_what_changed_and_asks_to_reload(
    tmp_path: Path, kind: str, subject: str
) -> None:
    """The 409 is worded from the catalogue: what changed, nothing saved, reload."""

    title = run_app_js(
        "return describeError({ status: 409, detail: { message: 'x', code: 'DB.conflict', "
        f"params: {{ kind: '{kind}', id: 'x_1' }} }} }}).title;",
        tmp_path,
    )
    assert title.startswith(f"{subject} wurde inzwischen von jemand anderem geändert")
    assert "gespeichert wurde nichts" in title
    assert "neu laden" in title


# --- the escalation sweep ------------------------------------------------------


def _escalating_case(name: str) -> tuple[str, str]:
    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Bearbeiten", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Bearbeiten")
    staff_via_api(client, sid)
    client.post(
        f"/schemas/{sid}/time-constraint",
        json={"node_id": act, "constraint": {"target_lead_seconds": 60}},
    )
    resp = client.post(
        f"/schemas/{sid}/escalation-policy",
        json={
            "node_id": act,
            "policy": {
                "stages": [
                    {
                        "after_seconds": 0,
                        "kind": "FUNCTIONAL",
                        "rule": {"kind": "ROLE", "ref": "r-test"},
                    }
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return _start(sid), act


def test_the_sweep_skips_a_case_that_changed_under_it(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep runs on every worklist read; it must never save over a change."""

    iid, act = _escalating_case(f"Eskalation im Wettlauf ({backend})")
    activated = api_module._instances.get(iid)
    assert activated is not None
    due = activated.node_activated_at[act] + timedelta(seconds=61)
    original = api_module._fire_escalation_stage

    def fire_while_someone_else_saves(*args: Any, **kwargs: Any) -> None:
        def other_writer() -> None:
            stored = api_module._instances.get(iid)
            assert stored is not None
            stored.data_values["marker"] = "von nebenan"
            api_module._instances.put(stored)

        writer = threading.Thread(target=other_writer)
        writer.start()
        writer.join()
        original(*args, **kwargs)

    monkeypatch.setattr(api_module, "_fire_escalation_stage", fire_while_someone_else_saves)

    assert api_module._escalation_sweep(now=due) == 0

    stored = api_module._instances.get(iid)
    assert stored is not None
    assert stored.data_values["marker"] == "von nebenan"
    assert stored.escalated_stages.get(act, 0) == 0
    if backend == "sqlite":
        assert _events(iid, EventType.TASK_ESCALATED) == []

    # The next sweep (the next worklist read) catches up on the fresh state.
    monkeypatch.setattr(api_module, "_fire_escalation_stage", original)
    assert api_module._escalation_sweep(now=due) == 1



# --- the repetition itself ------------------------------------------------------


def _deadlock() -> DBAPIError:
    """The error PostgreSQL raises for the loser of a deadlock (SQLSTATE 40P01)."""

    class _Driver(Exception):
        sqlstate = "40P01"

    return OperationalError("UPDATE process_instance ...", {}, _Driver("deadlock detected"))


def test_a_deadlocked_unit_is_repeated_like_a_conflict() -> None:
    attempts: list[int] = []

    def change() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise _deadlock()
        return "done"

    assert unit_of_work.run(change) == "done"
    assert len(attempts) == 2


def test_other_database_errors_are_not_repeated() -> None:
    attempts: list[int] = []

    def change() -> str:
        attempts.append(1)
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    with pytest.raises(OperationalError):
        unit_of_work.run(change)
    assert len(attempts) == 1


def test_a_unit_with_an_irreversible_effect_is_not_repeated() -> None:
    """After a delivery or an external write the caller hears of the conflict."""

    attempts: list[int] = []

    def change() -> str:
        attempts.append(1)
        unit_of_work.mark_irreversible()
        raise ConcurrentModification("instance", "instance_9")

    with pytest.raises(ConcurrentModification):
        unit_of_work.run(change)
    assert len(attempts) == 1


def test_a_conflict_swallowed_inside_the_unit_still_repeats_it() -> None:
    """Best-effort hooks catch every exception; the unit still learns of it."""

    store = InMemoryInstanceStore()
    store.put(_instance())
    stale = store.get("instance_1")
    fresh = store.get("instance_1")
    assert stale is not None and fresh is not None
    store.put(fresh)
    attempts: list[int] = []

    def change() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            try:
                store.put(stale)
            except ConcurrentModification:
                pass  # a hook that swallows everything
        return "done"

    assert unit_of_work.run(change) == "done"
    assert len(attempts) == 2


def test_every_mutating_endpoint_runs_as_a_unit_of_work() -> None:
    """A new router or endpoint cannot slip past the transaction and the repetition."""

    import importlib

    from fastapi.routing import APIRoute

    routers = [
        importlib.import_module(f"procworks.{name}").router
        for name in (
            "api_admin", "api_changes", "api_execution", "api_licensing", "api_modelling",
            "api_monitoring", "api_org", "api_schemas", "api_v1",
        )
    ]
    routers.append(importlib.import_module("procworks.api_v1").instance_data_router)
    routes = [r for router in routers for r in router.routes]
    routes += [r for r in app.routes if isinstance(r, APIRoute)]
    own_units: set[str] = set()
    checked = 0
    for route in routes:
        assert isinstance(route, api_module.TransactionalRoute), route.path
        if not route.methods & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        original = getattr(route.endpoint, "__wrapped__", route.endpoint)
        if getattr(original, "_procworks_own_units", False):
            own_units.add(route.path)
            assert route.endpoint is original  # not wrapped
            continue
        assert route.endpoint is not original, route.path  # wrapped in a unit
        checked += 1
    assert checked > 50
    # Each one outside the enclosing unit for a reason (see api.manages_own_units):
    assert own_units == {
        # its job is the delivery, or it forms one unit per case
        "/admin/mail-outbox/dispatch",
        "/admin/escalations/sweep",
        "/v1/external-tasks/drive-push",
        "/v1/webhooks/{subscription_id}/test",
        # stores nothing, waits for an outside system or the resolver
        "/v1/connectors/{connector_id}/test",
        "/v1/connectors/{connector_id}/sample-read",
        "/v1/webhooks/preview",
        # slow work first, then only the store part as a unit (api._run_as_unit)
        "/v1/webhooks",
        "/auth/login",
        "/auth/change-password",
        "/users",
        "/users/{login}/reset-password",
    }
