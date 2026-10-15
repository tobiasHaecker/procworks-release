# SPDX-License-Identifier: BUSL-1.1
"""``Idempotency-Key`` values are reserved where every API process sees them.

With one API process a lock per key was enough. With several processes on
one database (Helm ``replicaCount`` > 1) each process had its own memory:
two first requests with the same key, sent to two processes at the same
moment, both ran -- two instances for one key.

Pinned here:

* **Database store** (:class:`procworks.db.SqlAlchemyIdempotencyStore`): the
  key is reserved by a row in the request's transaction, the reference
  (object id and status -- never the answer) is written into the same
  transaction. A second store on the same database -- another process --
  replays a committed reference, finds the key free after a rollback or a
  refused request, waits while the first transaction runs, and treats an
  expired key as free.
* **Data minimisation and keyed digests**: the table holds neither the
  answer nor key, login or request in plain; key and request are HMACs with
  the server secret, so a digest computed without it (plain SHA-256) matches
  nothing. All processes share the installation's secret (from the database,
  or ``PROCWORKS_IDEMPOTENCY_SECRET``, which must be long enough).
* **The API on the database store**: a replay of the same request answers
  with the object's *current* state and the same status, ``IDEM.key-reused``
  for another request, a long key.
* **Housekeeping**: expired rows go also when no new key arrives (the API's
  timer calls ``remove_expired``).
* **In memory**: the same contract within one process, with expiry.
* **Migration** ``0021``: reshapes the table, creates the secret, removes
  the old rows that held plain answers; the downgrade restores the old shape.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from sqlalchemy import create_engine, insert, inspect, select, text
from staffing import staff_via_api

import procworks.api as api_module
from procworks import unit_of_work
from procworks.api import app
from procworks.db import IdempotencyKeyRow, ServerSecretRow, SqlAlchemyIdempotencyStore
from procworks.idempotency import (
    InMemoryIdempotencyStore,
    Replay,
    Reservation,
    keyed_digest,
    secret_from_env,
)

client = TestClient(app)


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'idem.db'}"


def _process(url: str) -> SqlAlchemyIdempotencyStore:
    """A store as another API process holds it (own object, same database)."""

    return SqlAlchemyIdempotencyStore(url, create_tables=True)


def _run(store: Any, fingerprint: str, answer: str | None = None,
         fail: BaseException | None = None, status: int = 201) -> Replay | None:
    """One request in a unit of work: reserve, then remember or fail.

    :param answer: id of the object the request "created".
    :returns: ``previous`` of the reservation as seen by this request.
    """

    seen: list[Any] = []

    def change() -> None:
        reservation: Reservation = store.reserve("svc", "k-1", fingerprint)
        unit_of_work.on_finish(reservation.close)
        seen.append(reservation.previous)
        if reservation.previous is not None:
            return
        if fail is not None:
            reservation.abandon()
            raise fail
        assert answer is not None
        reservation.remember(answer, status)

    unit_of_work.run(change, commit_on=(HTTPException,))
    return seen[0]


def _all_rows(url: str) -> list[Any]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return list(conn.execute(select(IdempotencyKeyRow)).all())
    finally:
        engine.dispose()


def test_another_process_replays_the_committed_reference(url: str) -> None:
    assert _run(_process(url), "fp", "instance_1") is None

    assert _run(_process(url), "fp") == Replay(True, "instance_1", 201)


def test_another_request_with_the_key_is_told_apart(url: str) -> None:
    """Negativ: a different request under the same key is no replay."""

    _run(_process(url), "fp", "instance_1")

    assert _run(_process(url), "anders") == Replay(False, "instance_1", 201)


def test_a_rolled_back_request_leaves_the_key_free(url: str) -> None:
    with pytest.raises(RuntimeError, match="kaputt"):
        _run(_process(url), "fp", fail=RuntimeError("kaputt"))

    assert _run(_process(url), "fp", "x") is None


def test_a_refused_request_leaves_the_key_free_although_its_unit_commits(url: str) -> None:
    """A deliberate refusal commits the unit's earlier writes -- not the key."""

    with pytest.raises(HTTPException):
        _run(_process(url), "fp", fail=HTTPException(status_code=409, detail="nein"))

    assert _all_rows(url) == []  # no reservation without a reference stays behind
    assert _run(_process(url), "fp", "y") is None


def test_a_second_process_waits_for_the_first_and_then_replays(url: str) -> None:
    first, second = _process(url), _process(url)
    reserved = threading.Event()
    answers: list[Any] = []

    def slow_first() -> None:
        def change() -> None:
            reservation = first.reserve("svc", "k-1", "fp")
            unit_of_work.on_finish(reservation.close)
            reserved.set()
            time.sleep(0.4)  # the operation runs; the second request arrives
            reservation.remember("instance_7", 201)

        unit_of_work.run(change)

    thread = threading.Thread(target=slow_first)
    thread.start()
    assert reserved.wait(5)
    answers.append(_run(second, "fp", "instance_8"))
    thread.join(10)

    assert answers == [Replay(True, "instance_7", 201)]


def test_an_expired_key_is_free_again(url: str) -> None:
    store = _process(url)
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(insert(IdempotencyKeyRow).values(
            key_digest=store._digest("svc", "k-1"), fingerprint="alt",
            object_id="alt", status=201, expires_at=datetime.now(UTC) - timedelta(seconds=1),
        ))
    engine.dispose()

    assert _run(store, "neu", "neu") is None
    assert _run(_process(url), "neu") == Replay(True, "neu", 201)


def test_only_expired_rows_are_removed(url: str) -> None:
    store = _process(url)
    _run(store, "fp", "frisch")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(insert(IdempotencyKeyRow).values(
            key_digest="abgelaufen", fingerprint="x", object_id="x", status=201,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        ))

    assert store.remove_expired() == 1

    with engine.connect() as conn:
        left = list(conn.execute(select(IdempotencyKeyRow.key_digest)).scalars())
    engine.dispose()
    assert left == [store._digest("svc", "k-1")]


def test_only_a_reference_and_keyed_digests_are_stored(url: str) -> None:
    """Nothing in plain, no answer -- and an unsalted hash matches nothing."""

    _run(_process(url), "fp-geheim", "instance_3", status=201)
    [row] = _all_rows(url)
    columns = set(row._mapping.keys())

    assert "answer" not in columns
    assert (row.object_id, row.status) == ("instance_3", 201)
    for value in (row.key_digest, row.fingerprint):
        assert len(value) == 64
        assert "svc" not in value and "k-1" not in value and "geheim" not in value
    unsalted = hashlib.sha256(b"svc\x1fk-1").hexdigest()
    assert row.key_digest != unsalted
    assert row.fingerprint != hashlib.sha256(b"fp-geheim").hexdigest()


def test_every_process_uses_the_installation_secret(url: str) -> None:
    first, second = _process(url), _process(url)
    engine = create_engine(url)
    with engine.connect() as conn:
        stored = conn.execute(select(ServerSecretRow.value)).scalars().all()
    engine.dispose()

    assert len(stored) == 1 and len(stored[0]) == 64
    assert first._digest("svc", "k") == second._digest("svc", "k")
    assert first._digest("svc", "k") == keyed_digest(stored[0].encode(), "svc", "k")


def test_the_secret_from_the_environment_wins(
    url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "s" * 40
    monkeypatch.setenv("PROCWORKS_IDEMPOTENCY_SECRET", f"  {secret}  ")

    store = SqlAlchemyIdempotencyStore(url, create_tables=True)

    assert store._digest("svc", "k") == keyed_digest(secret.encode(), "svc", "k")


@pytest.mark.parametrize("value", ["kurz", "x" * 31])
def test_a_short_secret_aborts_the_start(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Negativ: the reason names the variable and the length -- never the value."""

    monkeypatch.setenv("PROCWORKS_IDEMPOTENCY_SECRET", value)

    with pytest.raises(ValueError, match="PROCWORKS_IDEMPOTENCY_SECRET.*32") as exc:
        secret_from_env()
    assert value not in str(exc.value)


def test_an_empty_secret_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grenzfall: Compose passes an unset variable as empty."""

    monkeypatch.setenv("PROCWORKS_IDEMPOTENCY_SECRET", "   ")
    assert secret_from_env() is None


# --- the API on the database store ----------------------------------------------------


def _released_schema(name: str) -> str:
    sid = client.post("/schemas", json={"name": name}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "Tun", "after_node_id": "start"})
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return sid


def test_the_api_replays_from_the_database_and_refuses_another_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_sql_backend(monkeypatch, tmp_path)
    assert isinstance(api_module._idempotency, SqlAlchemyIdempotencyStore)
    sid = _released_schema("Einmal")
    other = _released_schema("Anderes")
    key = {"Idempotency-Key": "x" * 5000}  # long keys are fine (stored as digest)

    first = client.post(f"/v1/schemas/{sid}/instances", headers=key)
    again = client.post(f"/v1/schemas/{sid}/instances", headers=key)
    reused = client.post(f"/v1/schemas/{other}/instances", headers=key)

    assert (first.status_code, again.status_code) == (201, 201)
    assert again.json() == first.json()
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "IDEM.key-reused"
    started = [i for i in api_module._instances.list_ids()
               if (inst := api_module._instances.get(i)) and inst.schema_id in (sid, other)]
    assert len(started) == 1


def test_a_replay_shows_the_current_state_with_the_same_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reference is read anew: what happened since the first answer shows."""

    use_sql_backend(monkeypatch, tmp_path)
    sid = _released_schema("Neu gelesen")
    key = {"Idempotency-Key": "neu-gelesen"}
    first = client.post(f"/v1/schemas/{sid}/instances", headers=key)
    iid = first.json()["id"]
    task = next(n for n, st in first.json()["node_states"].items() if st == "ACTIVATED")
    # Change the case behind the first answer (its step is done meanwhile).
    done = client.post(f"/v1/instances/{iid}/nodes/{task}/complete", json={})
    assert done.status_code == 200, done.text

    again = client.post(f"/v1/schemas/{sid}/instances", headers=key)

    assert again.status_code == 201
    assert again.json()["id"] == iid
    assert again.json()["node_states"][task] == "COMPLETED"
    assert first.json()["node_states"][task] == "ACTIVATED"


def test_expired_rows_go_without_a_new_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The housekeeping pass of the API removes them -- no reservation needed."""

    use_sql_backend(monkeypatch, tmp_path)
    store = api_module._idempotency
    assert isinstance(store, SqlAlchemyIdempotencyStore)
    url = f"sqlite:///{tmp_path / 'procworks.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(insert(IdempotencyKeyRow).values(
            key_digest="alt", fingerprint="x", object_id="x", status=201,
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        ))
    engine.dispose()

    assert api_module._idempotency_housekeeping_pass() is False

    assert _all_rows(url) == []


# --- in memory ------------------------------------------------------------------------


def test_in_memory_the_contract_holds_with_expiry() -> None:
    now = [1000.0]
    store = InMemoryIdempotencyStore(ttl_seconds=60, clock=lambda: now[0])

    with pytest.raises(RuntimeError):
        _run(store, "fp", fail=RuntimeError("x"))
    assert _run(store, "fp", "a") is None  # the failure kept nothing
    assert _run(store, "fp") == Replay(True, "a", 201)
    assert _run(store, "anders") == Replay(False, "a", 201)
    now[0] += 61
    assert store.remove_expired() == 1
    assert _run(store, "fp2", "b") is None  # expired: free again


def test_the_migration_reshapes_the_table_and_the_downgrade_restores_it(tmp_path: Path) -> None:
    pytest.importorskip("alembic")
    core = Path(__file__).resolve().parents[1]
    db_url = f"sqlite:///{tmp_path / 'mig.db'}"

    def alembic(*args: str) -> None:
        env = {**os.environ, "DATABASE_URL": db_url, "PYTHONPATH": str(core / "src")}
        env.pop("PROCWORKS_IDEMPOTENCY_SECRET", None)
        done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=core, env=env,
                              capture_output=True, text=True, timeout=120, check=False)
        assert done.returncode == 0, done.stderr

    def columns(table: str) -> set[str]:
        engine = create_engine(db_url)
        try:
            inspector = inspect(engine)
            if table not in inspector.get_table_names():
                return set()
            return {c["name"] for c in inspector.get_columns(table)}
        finally:
            engine.dispose()

    def execute(sql: str) -> list[Any]:
        engine = create_engine(db_url)
        try:
            with engine.begin() as conn:
                result = conn.execute(text(sql))
                return list(result.all()) if result.returns_rows else []
        finally:
            engine.dispose()

    # The old shape with a row that holds a plain answer.
    alembic("upgrade", "0020_login_agent_unique")
    assert "answer" in columns("idempotency_key")
    execute(
        "INSERT INTO idempotency_key (key_digest, fingerprint, answer, expires_at) "
        "VALUES ('alt', 'fp', '{\"data_values\": {\"iban\": \"DE00\"}}', '2999-01-01')"
    )

    alembic("upgrade", "head")
    assert columns("idempotency_key") >= {"key_digest", "fingerprint", "object_id", "status"}
    assert "answer" not in columns("idempotency_key")
    assert execute("SELECT count(*) FROM idempotency_key") == [(0,)]  # plain answer gone
    [(name, value)] = execute("SELECT name, value FROM server_secret")
    assert name == "idempotency" and len(value) == 64
    store = SqlAlchemyIdempotencyStore(db_url)  # no create_all: the migrated tables
    assert _run(store, "fp", "m") is None
    assert _run(SqlAlchemyIdempotencyStore(db_url), "fp") == Replay(True, "m", 201)

    alembic("downgrade", "0020_login_agent_unique")
    assert "answer" in columns("idempotency_key")
    assert not {"object_id", "status"} & columns("idempotency_key")
    assert columns("server_secret") == set()
    assert execute("SELECT count(*) FROM idempotency_key") == [(0,)]

    alembic("upgrade", "head")  # and up again
    alembic("downgrade", "0018_org_lock_version")
    assert columns("idempotency_key") == set()
    assert columns("org_model")
