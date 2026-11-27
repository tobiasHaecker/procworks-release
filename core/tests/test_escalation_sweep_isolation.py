# SPDX-License-Identifier: BUSL-1.1
"""One broken case never stops the escalation of the others.

The sweep escalates every due case in a unit of its own. It used to catch
only a conflict of the conditional save: any other error of one case -- a
damaged mail outbox record that no longer loads, a database deadlock --
ended the whole pass. The cases are read in a stable
order, so a permanently broken case stopped every later pass at the same
place, and every due case behind it never escalated (only a log line said
so).

Now each case is isolated: an error is logged with the case id, the case is
skipped (the next pass tries again), and the pass goes on. The admin trigger
reports how many cases it skipped.

Isolated from the other cases, not from the database: an outage (a lost
connection, a statement the server cancelled, an exhausted pool) is no error
of one case. It ends the pass, and the admin trigger answers 503 like every
other request -- instead of 200 with every due case "skipped".
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from staffing import staff_via_api

import procworks.api as api_module
from procworks import execution as exe
from procworks.api import app
from procworks.store import InMemoryInstanceStore

client = TestClient(app)


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Run the test once on in-memory stores and once on SQLite.

    Both with a case store of their own: the cases these tests leave due on
    purpose (the broken ones) must not be escalated by a later test's sweep.
    """

    if request.param == "sqlite":
        use_sql_backend(monkeypatch, tmp_path)
    else:
        _own_memory_cases(monkeypatch)
    return str(request.param)


def _own_memory_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the API module a fresh in-memory case store for this test."""

    instances = InMemoryInstanceStore()
    monkeypatch.setattr(api_module, "_instances", instances)
    monkeypatch.setattr(
        api_module,
        "_context",
        exe.ExecutionContext(
            api_module._resolver,
            instances,
            on_spawn=lambda spawned, origin: api_module._record_spawned_instance(
                spawned, origin
            ),
        ),
    )


def _escalating_cases(name: str, count: int) -> tuple[list[str], str]:
    """A released schema whose only step escalates at once, and ``count`` cases."""

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
    ids = []
    for _ in range(count):
        started = client.post(f"/schemas/{sid}/instances")
        assert started.status_code == 201, started.text
        ids.append(str(started.json()["id"]))
    return ids, act


def _due_moment(iid: str, act: str) -> Any:
    stored = api_module._instances.get(iid)
    assert stored is not None
    return stored.node_activated_at[act] + timedelta(seconds=61)


def _stage(iid: str, act: str) -> int:
    stored = api_module._instances.get(iid)
    assert stored is not None
    return stored.escalated_stages.get(act, 0)


def _break_case(monkeypatch: pytest.MonkeyPatch, broken: str, error: Exception) -> None:
    """Let the escalation of case ``broken`` fail with ``error`` (others pass)."""

    original = api_module._fire_escalation_stage

    def fire(instance: Any, *args: Any, **kwargs: Any) -> None:
        if instance.id == broken:
            raise error
        original(instance, *args, **kwargs)

    monkeypatch.setattr(api_module, "_fire_escalation_stage", fire)


def test_a_broken_case_is_skipped_and_logged_and_the_others_escalate(
    backend: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ids, act = _escalating_cases(f"Eskalation mit defektem Fall ({backend})", 4)
    broken = ids[1]
    _break_case(monkeypatch, broken, ValueError("stored mail record does not load"))
    moment = max(_due_moment(i, act) for i in ids)

    with caplog.at_level(logging.WARNING, logger="procworks.api"):
        fired = api_module._escalation_sweep(now=moment)

    assert fired == 3
    assert [_stage(i, act) for i in ids] == [1, 0, 1, 1]
    logged = [r for r in caplog.records if broken in r.getMessage()]
    assert logged and "stored mail record does not load" in logged[0].getMessage()


def test_a_permanently_broken_case_blocks_no_later_pass(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids, act = _escalating_cases(f"Eskalation, dauerhaft defekt ({backend})", 2)
    broken = ids[0]  # first in the stable reading order: it would stop every pass
    _break_case(monkeypatch, broken, ValueError("broken for good"))
    moment = max(_due_moment(i, act) for i in ids)

    assert api_module._escalation_sweep(now=moment) == 1
    # A case that became due later is reached by the next pass, too.
    [late], _ = _escalating_cases(f"Eskalation, spaeter faellig ({backend})", 1)
    assert api_module._escalation_sweep(now=_due_moment(late, _act_of(late))) == 1
    assert _stage(broken, act) == 0
    assert _stage(late, _act_of(late)) == 1


def _act_of(iid: str) -> str:
    stored = api_module._instances.get(iid)
    assert stored is not None
    return next(iter(stored.node_activated_at))


def test_a_deadlock_skips_the_case_and_the_admin_trigger_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database collision (SQLSTATE 40P01) is no reason to answer 503."""

    _own_memory_cases(monkeypatch)

    class _Driver(Exception):
        sqlstate = "40P01"

    ids, act = _escalating_cases("Eskalation mit Deadlock", 3)
    deadlock = OperationalError("UPDATE audit_chain ...", {}, _Driver("deadlock detected"))
    _break_case(monkeypatch, ids[2], deadlock)
    # Every case is due by now: the trigger sweeps with the real clock.
    for iid in ids:
        stored = api_module._instances.get(iid)
        assert stored is not None
        stored.node_activated_at[act] -= timedelta(seconds=120)
        api_module._instances.put(stored)

    resp = client.post("/admin/escalations/sweep")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"fired": 2, "skipped": 1}
    assert [_stage(i, act) for i in ids] == [1, 1, 0]


def test_an_interrupt_still_ends_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only errors of one case are isolated -- a shutdown is not swallowed."""

    _own_memory_cases(monkeypatch)
    ids, act = _escalating_cases("Eskalation, Abbruch", 1)

    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(api_module, "_fire_escalation_stage", interrupted)
    with pytest.raises(KeyboardInterrupt):
        api_module._escalation_sweep(now=_due_moment(ids[0], act))
    assert _stage(ids[0], act) == 0


def _due_now(ids: list[str], act: str) -> None:
    """Move every case's activation back, so the trigger (real clock) finds them due."""

    for iid in ids:
        stored = api_module._instances.get(iid)
        assert stored is not None
        stored.node_activated_at[act] -= timedelta(seconds=120)
        api_module._instances.put(stored)


class _Cancelled(Exception):
    """Driver error of a statement the server cancelled (statement timeout)."""

    sqlstate = "57014"


@pytest.mark.parametrize(
    "outage",
    [
        OperationalError("SELECT ...", {}, Exception("connection refused")),
        OperationalError("UPDATE process_instance ...", {}, _Cancelled("statement timeout")),
        PoolTimeoutError("QueuePool limit reached"),
    ],
    ids=["connection-lost", "statement-timeout", "pool-exhausted"],
)
def test_a_database_outage_ends_the_pass_and_the_trigger_answers_503(
    monkeypatch: pytest.MonkeyPatch, outage: Exception
) -> None:
    """An outage is no error of one case: the pass stops, the trigger says so.

    Isolating it per case turned a database outage into ``200 {"skipped": N}``
    -- one failing transaction per due case, and the operator saw routine
    skips. Only a collision of one case (deadlock, serialisation failure, a
    changed case) and a fault of the case itself are skipped.
    """

    _own_memory_cases(monkeypatch)
    ids, act = _escalating_cases(f"Eskalation bei Ausfall ({type(outage).__name__})", 3)
    attempted: list[str] = []
    original = api_module._fire_escalation_stage

    def fire(instance: Any, *args: Any, **kwargs: Any) -> None:
        attempted.append(instance.id)
        if instance.id == ids[0]:
            raise outage
        original(instance, *args, **kwargs)

    monkeypatch.setattr(api_module, "_fire_escalation_stage", fire)
    _due_now(ids, act)

    resp = client.post("/admin/escalations/sweep")

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "DB.unavailable"
    assert attempted == [ids[0]]  # no further case is tried during the outage
    assert [_stage(i, act) for i in ids] == [0, 0, 0]


def test_a_database_outage_ends_the_background_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _own_memory_cases(monkeypatch)
    ids, act = _escalating_cases("Eskalation im Hintergrund bei Ausfall", 2)
    _break_case(monkeypatch, ids[0], OperationalError("SELECT ...", {}, Exception("gone")))

    with pytest.raises(OperationalError):
        api_module._escalation_sweep(now=max(_due_moment(i, act) for i in ids))
    assert [_stage(i, act) for i in ids] == [0, 0]
