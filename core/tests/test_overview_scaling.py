# SPDX-License-Identifier: BUSL-1.1
"""Overview reads stay flat in the stock; escalations run in the background.

Worklists, the monitoring and the start of a case used to load every case one
by one -- and before each worklist read the escalation sweep loaded every case
*again*, together with its schema and organisation. The monitoring asked for
the tasks of each running case separately, so its load grew with the square
of the stock (minutes at a few hundred running cases), and every worklist and
every start grew linearly.

Pinned here:

* **constant work per request** -- on SQL stores the number of statements of
  every overview read (personal worklist, monitoring cases/tasks, titles,
  unstaffed steps) and of a start does not grow when more cases of the same
  schema are running; the reading right of a limited operator does not load
  the history;
* **reads never escalate** -- the sweep is not called by a read, a due stage
  is not fired by it;
* **the sweep only touches schemas with escalation rules and is bounded** per
  run;
* **the background timer** fires due stages without any read, stops with the
  application, can be switched off, refuses an invalid interval with the
  variable's name; two timers (two API processes) escalate a case once;
* **the collective monitoring reads say the same** as the per-case reads they
  replace (limited operator, test instances, empty stock);
* a generous time bound for the overview reads at a stock of a few hundred
  running cases.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from planted import plant
from sql_backed_api import use_sql_backend
from sqlalchemy import event
from staffing import TEST_AGENT_ID, staff_via_api

import procworks.api as api_module
from procworks import api_execution, api_monitoring
from procworks.audit import EventType
from procworks.auth import Principal

client = TestClient(api_module.app)

#: The environment variable of the background escalation timer.
_INTERVAL_VAR = "PROCWORKS_ESCALATION_SWEEP_SECONDS"


# --- fixtures ------------------------------------------------------------------


@pytest.fixture
def sql_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """The API on SQLAlchemy stores of a fresh SQLite file (isolated stock)."""

    return use_sql_backend(monkeypatch, tmp_path)


@contextmanager
def _counted_statements() -> Iterator[list[str]]:
    """Collect every SQL statement the API's shared engine executes meanwhile."""

    engine = api_module._instances._engine  # type: ignore[attr-defined]  # SQL store in these tests
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _one_step_schema(name: str) -> tuple[str, str]:
    """Released schema start -> "Bearbeiten" (role r-test: a-test) -> end."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Bearbeiten", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Bearbeiten")
    staff_via_api(client, sid)
    return sid, act


def _release(sid: str) -> None:
    resp = client.post(f"/schemas/{sid}/release")
    assert resp.status_code == 200, resp.text


def _plain_schema(name: str) -> tuple[str, str]:
    sid, act = _one_step_schema(name)
    _release(sid)
    return sid, act


def _escalating_schema(name: str) -> tuple[str, str]:
    """Like :func:`_plain_schema`, with a one-stage FUNCTIONAL escalation 60 s after due."""

    sid, act = _one_step_schema(name)
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
    _release(sid)
    return sid, act


def _start(sid: str, count: int = 1) -> list[str]:
    ids = []
    for _ in range(count):
        resp = client.post(f"/schemas/{sid}/instances")
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])
    return ids


def _backdate(instance_id: str, node_id: str, hours: float = 2) -> None:
    """Move the activation of ``node_id`` into the past, so its step is overdue now."""

    stored = api_module._instances.get(instance_id)
    assert stored is not None
    stored.node_activated_at[node_id] = datetime.now(UTC) - timedelta(hours=hours)
    plant(api_module._instances, stored)


def _escalations(instance_id: str) -> int:
    return sum(
        1
        for e in api_module._audit.for_instance(instance_id)
        if e.event_type is EventType.TASK_ESCALATED
    )


#: The collective reads of the overviews (open mode: every role).
_OVERVIEW_READS = (
    f"/agents/{TEST_AGENT_ID}/tasks",
    "/monitoring/tasks",
    "/monitoring/instances",
    "/monitoring/unstaffed",
    "/instance-titles",
    "/instances",
)


# --- constant work per request ---------------------------------------------------


@pytest.mark.parametrize("path", _OVERVIEW_READS)
def test_an_overview_read_issues_as_many_statements_at_25_cases_as_at_5(
    sql_api: str, path: str
) -> None:
    """The statements of one overview read do not grow with the running cases.

    Before, each read loaded every case and its schema separately (two
    statements per case) and the worklists additionally ran the sweep over all
    cases -- the read grew linearly, the monitoring (one task read per case)
    quadratically.
    """

    sid, _act = _plain_schema("Viele Vorgaenge")
    _start(sid, 5)
    with _counted_statements() as few:
        assert client.get(path).status_code == 200
    _start(sid, 20)
    with _counted_statements() as many:
        resp = client.get(path)
    assert resp.status_code == 200
    assert len(many) == len(few), (
        f"{path}: {len(few)} statements at 5 cases, {len(many)} at 25 -- "
        "the read loads cases (or schemas) one by one"
    )


def test_a_start_issues_as_many_statements_at_25_cases_as_at_5(sql_api: str) -> None:
    """Starting a case does not visit the other running cases one by one.

    The push drive after every advance looked at every running case (case
    and schema loaded separately) to find newly activated push steps; a
    start got slower with every case in the stock.
    """

    sid, _act = _plain_schema("Start bei Bestand")
    _start(sid, 5)
    with _counted_statements() as few:
        _start(sid)
    _start(sid, 20)
    with _counted_statements() as many:
        _start(sid)
    assert len(many) == len(few), (
        f"start: {len(few)} statements at 5 cases, {len(many)} at 25"
    )


def test_the_reading_right_of_a_limited_operator_neither_loads_the_history_nor_case_by_case(
    sql_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The involvement of a limited operator is decided with collective reads.

    It used to read the whole audit history and every running case
    separately on every list request.
    """

    monkeypatch.setattr(api_module, "_auth_mode", lambda: "password")
    operator = Principal(subject="t", agent_id=TEST_AGENT_ID, roles=frozenset({"operator"}))
    assert api_module._reads_only_own_instances(operator)
    sid, _act = _plain_schema("Beteiligung")
    ids = _start(sid, 5)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the whole history or a single case was loaded")

    monkeypatch.setattr(api_module._audit, "list_all", forbidden)
    monkeypatch.setattr(api_module._instances, "get", forbidden)
    involved = api_module._involved_instance_ids(operator)
    assert set(ids) <= involved  # eligible for the open step of each


# --- reads never escalate ----------------------------------------------------------


def test_reading_worklists_does_not_run_the_sweep(
    sql_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep is the background timer's job; a read must not trigger it."""

    sid, act = _escalating_schema("Lesen ohne Abgleich")
    (iid,) = _start(sid)
    _backdate(iid, act)
    calls: list[object] = []
    monkeypatch.setattr(api_module, "_escalation_sweep", lambda *a, **k: calls.append(a) or 0)

    for path in (f"/instances/{iid}/tasks", f"/agents/{TEST_AGENT_ID}/tasks", "/me/tasks",
                 "/monitoring/tasks"):
        assert client.get(path).status_code == 200, path
    assert calls == [], "a read ran the escalation sweep"


def test_a_due_stage_is_not_fired_by_a_read_but_by_the_sweep(sql_api: str) -> None:
    sid, act = _escalating_schema("Faellig beim Lesen")
    (iid,) = _start(sid)
    _backdate(iid, act)

    tasks = client.get(f"/instances/{iid}/tasks").json()
    client.get(f"/agents/{TEST_AGENT_ID}/tasks")
    assert [t["escalated_stage"] for t in tasks if t["node_id"] == act] == [0]
    assert _escalations(iid) == 0, "the read fired the due stage"

    assert api_module._escalation_sweep() == 1
    assert _escalations(iid) == 1
    tasks = client.get(f"/instances/{iid}/tasks").json()
    assert [t["escalated_stage"] for t in tasks if t["node_id"] == act] == [1]


# --- the sweep: only schemas with rules, bounded -------------------------------------


def test_the_sweep_only_looks_at_cases_of_schemas_with_escalation_rules(
    sql_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain_sid, _ = _plain_schema("Ohne Regeln")
    esc_sid, act = _escalating_schema("Mit Regeln")
    _start(plain_sid, 6)
    (due,) = _start(esc_sid)
    _backdate(due, act)
    looked_at: list[str] = []
    original = api_module._due_escalations

    def spy(instance: Any, moment: datetime, *rest: Any) -> Any:
        looked_at.append(instance.schema_id)
        return original(instance, moment, *rest)

    monkeypatch.setattr(api_module, "_due_escalations", spy)
    assert api_module._escalation_sweep() == 1
    assert plain_sid not in looked_at, "the sweep evaluated cases of a schema without rules"
    assert esc_sid in looked_at


def test_the_sweep_does_not_load_a_case_without_rules_even_to_check_it(
    sql_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deciding on the schemas comes first: without any rule no case is read."""

    plain_sid, _ = _plain_schema("Nur ohne Regeln")
    _start(plain_sid, 4)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the sweep loaded a case although no schema has rules")

    monkeypatch.setattr(api_module._instances, "get", forbidden)
    monkeypatch.setattr(api_module._instances, "list_instances", forbidden)
    assert api_module._escalation_sweep() == 0


def test_the_sweep_is_bounded_per_run_and_the_next_run_continues(sql_api: str) -> None:
    sid, act = _escalating_schema("Begrenzt")
    ids = _start(sid, 5)
    for iid in ids:
        _backdate(iid, act)

    assert api_module._escalation_sweep(limit=2) == 2
    assert api_module._escalation_sweep(limit=2) == 2
    assert api_module._escalation_sweep(limit=2) == 1
    assert api_module._escalation_sweep(limit=2) == 0
    assert [_escalations(i) for i in ids] == [1, 1, 1, 1, 1]


@pytest.mark.parametrize("limit", [0, -1])
def test_a_limit_below_one_is_refused(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        api_module._escalation_sweep(limit=limit)


def test_an_empty_stock_sweeps_nothing(sql_api: str) -> None:
    assert api_module._escalation_sweep() == 0
    assert client.get("/monitoring/tasks").json() == []
    assert client.get("/monitoring/instances").json() == []


# --- the background timer ------------------------------------------------------------


def _wait_for(condition: Callable[[], bool], seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def _timer_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "procworks-escalation-sweep"]


def test_the_background_timer_fires_a_due_stage_without_any_read(
    sql_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid, act = _escalating_schema("Im Takt")
    (iid,) = _start(sid)
    _backdate(iid, act)
    monkeypatch.setenv(_INTERVAL_VAR, "1")  # the smallest interval allowed

    with TestClient(api_module.app):
        assert len(_timer_threads()) == 1
        assert _wait_for(lambda: _escalations(iid) == 1), "the timer did not escalate"
    # Stopped with the application, and nothing fired twice.
    assert _wait_for(lambda: not _timer_threads(), 5.0), "the timer outlived the application"
    assert _escalations(iid) == 1


def test_interval_zero_switches_the_timer_off(
    sql_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid, act = _escalating_schema("Abgeschaltet")
    (iid,) = _start(sid)
    _backdate(iid, act)
    monkeypatch.setenv(_INTERVAL_VAR, "0")

    with TestClient(api_module.app):
        assert _timer_threads() == []
        time.sleep(0.3)
    assert _escalations(iid) == 0


@pytest.mark.parametrize("raw", ["abc", "-1", "nan", "inf"])
def test_an_invalid_interval_aborts_the_start_and_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(_INTERVAL_VAR, raw)
    with pytest.raises(ValueError, match=_INTERVAL_VAR), TestClient(api_module.app):
        pass
    assert _timer_threads() == []


def test_the_interval_defaults_to_a_minute_and_empty_counts_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from procworks import escalation_timer

    monkeypatch.delenv(_INTERVAL_VAR, raising=False)
    assert escalation_timer.sweep_interval() == 60.0
    monkeypatch.setenv(_INTERVAL_VAR, "  ")
    assert escalation_timer.sweep_interval() == 60.0
    monkeypatch.setenv(_INTERVAL_VAR, "7.5")
    assert escalation_timer.sweep_interval() == 7.5
    monkeypatch.setenv(_INTERVAL_VAR, "0")
    assert escalation_timer.sweep_interval() == 0.0


def test_two_timers_at_once_escalate_a_case_exactly_once(
    sql_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two API processes run a timer each; both may find the same due case.

    Forced overlap: both sweeps have read the case before either fires. The
    conditional save lets one of them store the escalation; the other's unit
    is rolled back (no second event, no second mail).
    """

    sid, act = _escalating_schema("Zwei Prozesse")
    (iid,) = _start(sid)
    _backdate(iid, act)
    barrier = threading.Barrier(2, timeout=2)
    original = api_module._fire_escalation_stage

    def fire_together(*args: Any, **kwargs: Any) -> None:
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        original(*args, **kwargs)

    monkeypatch.setattr(api_module, "_fire_escalation_stage", fire_together)
    results: list[int] = []
    threads = [
        threading.Thread(target=lambda: results.append(api_module._escalation_sweep()))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [0, 1]
    assert _escalations(iid) == 1
    stored = api_module._instances.get(iid)
    assert stored is not None and stored.escalated_stages[act] == 1


# --- the collective monitoring reads say the same ------------------------------------


def _task_key(task: dict[str, Any]) -> tuple[Any, ...]:
    return (
        task["instance_id"],
        task["node_id"],
        tuple(sorted(task["eligible_agents"])),
        task["escalated_stage"],
        task["claimed_by"],
        task["due_at"],
    )


def test_monitoring_tasks_equal_the_per_case_task_lists(sql_api: str) -> None:
    plain_sid, _ = _plain_schema("Gleichwertig A")
    esc_sid, act = _escalating_schema("Gleichwertig B")
    running = _start(plain_sid, 3) + _start(esc_sid, 2)
    _backdate(running[-1], act)
    api_module._escalation_sweep()
    # A test instance of a draft stays out, like everywhere in the monitoring.
    draft_sid, _ = _one_step_schema("Entwurf")
    test_iid = client.post(f"/schemas/{draft_sid}/instances").json()["id"]

    expected = sorted(
        _task_key(t) for iid in running for t in client.get(f"/instances/{iid}/tasks").json()
    )
    got = client.get("/monitoring/tasks").json()
    assert sorted(_task_key(t) for t in got) == expected
    assert test_iid not in {t["instance_id"] for t in got}
    assert any(t["escalated_stage"] == 1 for t in got)


def test_monitoring_instances_equal_the_readable_cases(sql_api: str) -> None:
    sid, _ = _plain_schema("Liste")
    ids = _start(sid, 3)
    listed = client.get("/instances").json()
    docs = client.get("/monitoring/instances").json()
    assert sorted(d["id"] for d in docs) == sorted(listed)
    by_id = {d["id"]: d for d in docs}
    for iid in ids:
        assert by_id[iid] == client.get(f"/instances/{iid}").json()


def test_a_limited_operator_gets_only_its_own_cases_and_tasks(
    sql_api: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: the collective reads keep the reading right (no foreign case)."""

    mine_sid, _ = _plain_schema("Meine")
    foreign_sid = client.post("/schemas", json={"name": "Fremde"}).json()["id"]
    schema = client.post(
        f"/schemas/{foreign_sid}/serial-insert",
        json={"label": "Fremd", "after_node_id": "start"},
    ).json()
    node = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Fremd")
    client.post(f"/schemas/{foreign_sid}/roles", json={"name": "Andere", "role_id": "r-x"})
    client.post(
        f"/schemas/{foreign_sid}/agents",
        json={"name": "Xaver", "role_ids": ["r-x"], "agent_id": "a-x"},
    )
    client.post(
        f"/schemas/{foreign_sid}/staff-rule",
        json={"node_id": node, "rule": {"kind": "ROLE", "ref": "r-x"}},
    )
    _release(foreign_sid)
    mine = _start(mine_sid, 2)
    foreign = _start(foreign_sid, 2)

    monkeypatch.setattr(api_module, "_auth_mode", lambda: "password")
    operator = Principal(subject="t", agent_id=TEST_AGENT_ID, roles=frozenset({"operator"}))
    cases = api_monitoring.get_monitoring_instances(operator)
    tasks = api_monitoring.get_monitoring_tasks(operator)
    titles = api_execution.get_instance_titles(operator)
    assert {c.id for c in cases} == set(mine)
    assert {t.instance_id for t in tasks} == set(mine)
    assert not set(foreign) & set(titles)


# --- generous time bound -------------------------------------------------------------


def test_overview_reads_stay_fast_at_a_few_hundred_running_cases() -> None:
    """A coarse guard (in memory, generous): 250 running cases, every
    overview read well under the targets' order of magnitude."""

    sid, _ = _plain_schema("Zeitschranke")
    _start(sid, 250)
    for path in _OVERVIEW_READS:
        started = time.perf_counter()
        assert client.get(path).status_code == 200
        elapsed = time.perf_counter() - started
        assert elapsed < 3.0, f"{path} took {elapsed:.2f}s at 250+ running cases"


def test_the_full_stack_compose_passes_the_sweep_interval_through() -> None:
    """Operators set the interval in ``deploy/.env``; Compose must hand it to
    the API container (empty = the default of 60 s)."""

    compose_file = Path(__file__).resolve().parents[2] / "deploy" / "docker-compose.full.yml"
    if not compose_file.is_file():  # pragma: no cover - repo layout guard
        pytest.skip("deploy/ not present in this checkout")
    compose = compose_file.read_text(encoding="utf-8")
    assert f'{_INTERVAL_VAR}: "${{{_INTERVAL_VAR}:-}}"' in compose
