# SPDX-License-Identifier: BUSL-1.1
"""Two API processes against one database never hand out the same id.

Each API process allocates ``instance_<n>``, ``schema_<n>``, ``tpl_<n>`` and
``org_<n>`` from its own counter. The start-up lift and the store lookup keep a
*single* process from reusing a stored id. With several processes (Kubernetes
replicas) both may find the same number unused in the same instant -- the
lookup happens before the save -- and because the stores save by upsert, the
later save would silently replace the earlier object.

The fix is an atomic reservation (``id_claim``, a unique insert) before an id
is handed out. Pinned here:

* the race itself, with two sequences standing in for two processes at the
  same counter and nothing saved yet (the dangerous window) -- once without a
  claim (they collide, which is exactly the defect) and once with a shared
  claim (they never do);
* the SQL claim store: exclusive across separate store objects on one
  database, i.e. across processes, and only a duplicate key counts as "taken";
  whether the INSERT won comes from ``RETURNING``, not from the driver's
  ``rowcount`` (SQLAlchemy 2.1 with psycopg 3 reports ``-1`` there, and a
  claim built on it called every fresh id "taken");
* the wiring in the API: every globally stored prefix is claimed, a reserved id
  is skipped by a real request, a restart lifts the counters past reserved ids,
  ``/admin/reset`` keeps the reservations (another process may still save one);
* every table of the ORM model has an Alembic migration -- ``id_claim`` would
  otherwise exist only where tables are created on first use.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import CursorResult
from staffing import staff_via_api

import procworks.api as api_module
from procworks import ids
from procworks.api import app
from procworks.db import Base, SqlAlchemyIdClaimStore
from procworks.store import InMemoryIdClaimStore

client = TestClient(app)

_MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"


def _process(start: int, claim: Callable[[str], bool] | None) -> ids.IdSequence:
    """A sequence like the one of a freshly started API process.

    :param start: the counter after the start-up lift; equal for both
        "processes" because both lifted past the same stored state.
    :param claim: the shared reservation (``claim`` method) or ``None``.
    :returns: a sequence whose store guard sees nothing stored yet -- the
        window between choosing an id and saving the object.
    """

    sequence = ids.IdSequence()
    sequence.reset_for_tests(start)
    sequence.guard("instance", lambda key: False, claim)
    return sequence


def test_without_a_claim_two_processes_hand_out_the_same_id() -> None:
    """The defect the claim closes: the store lookup alone lets both through."""

    first, second = _process(26, None), _process(26, None)

    assert first.new("instance") == second.new("instance") == "instance_26"


def test_with_a_shared_claim_two_processes_never_hand_out_the_same_id() -> None:
    """Alternating allocations of two processes stay disjoint and gap-free together."""

    claims = InMemoryIdClaimStore()
    first, second = _process(26, claims.claim), _process(26, claims.claim)

    handed_out = [seq.new("instance") for _ in range(25) for seq in (first, second)]

    assert len(set(handed_out)) == len(handed_out) == 50
    assert sorted(int(key.rsplit("_", 1)[1]) for key in handed_out) == list(range(26, 76))


def test_a_failing_claim_aborts_the_allocation_instead_of_skipping_it() -> None:
    """A database error must not turn into an unguarded id -- the request fails."""

    def broken(key: str) -> bool:
        raise RuntimeError("database unreachable")

    sequence = _process(1, broken)

    with pytest.raises(RuntimeError, match="database unreachable"):
        sequence.new("instance")


def test_removing_the_guard_removes_the_claim_too() -> None:
    sequence = _process(1, InMemoryIdClaimStore().claim)
    assert sequence.has_claim("instance")

    sequence.guard("instance", None)

    assert not sequence.has_claim("instance")


def test_sql_claim_is_exclusive_across_processes(tmp_path: Path) -> None:
    """Two store objects on one database behave like two API processes."""

    url = f"sqlite:///{tmp_path / 'claims.db'}"
    process_a = SqlAlchemyIdClaimStore(url, create_tables=True)
    process_b = SqlAlchemyIdClaimStore(url)

    assert process_a.claim("instance_7") is True
    assert process_b.claim("instance_7") is False
    assert process_a.claim("instance_7") is False
    assert process_b.claim("instance_8") is True
    assert sorted(process_a.list_ids()) == ["instance_7", "instance_8"]

    process_a.clear()

    assert process_b.claim("instance_7") is True


def test_sql_claims_keep_two_processes_apart(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'claims.db'}"
    claims_a = SqlAlchemyIdClaimStore(url, create_tables=True)
    claims_b = SqlAlchemyIdClaimStore(url)
    first, second = _process(40, claims_a.claim), _process(40, claims_b.claim)

    handed_out = [seq.new("instance") for _ in range(10) for seq in (first, second)]

    assert len(set(handed_out)) == 20


def test_sql_claim_reports_other_database_errors(tmp_path: Path) -> None:
    """Only a duplicate key means "taken"; a missing table is an error, not a skip."""

    store = SqlAlchemyIdClaimStore(f"sqlite:///{tmp_path / 'empty.db'}")  # no tables

    with pytest.raises(Exception, match="id_claim"):
        store.claim("instance_1")


@pytest.fixture
def unknown_rowcount(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every statement report ``rowcount == -1`` ("unknown").

    That is what SQLAlchemy 2.1 with psycopg 3 returns for ``INSERT ... ON
    CONFLICT DO NOTHING`` on PostgreSQL even when the row was inserted. SQLite
    reports the real count, so without this the defect stays invisible here.
    """

    monkeypatch.setattr(CursorResult, "rowcount", property(lambda self: -1))


@pytest.mark.usefixtures("unknown_rowcount")
def test_sql_claim_does_not_depend_on_the_driver_rowcount(tmp_path: Path) -> None:
    """A fresh id is reserved and a duplicate refused even when rowcount is ``-1``."""

    store = SqlAlchemyIdClaimStore(f"sqlite:///{tmp_path / 'claims.db'}", create_tables=True)

    assert store.claim("instance_1") is True  # was False: every id looked taken
    assert store.claim("instance_1") is False
    assert store.claim("instance_2") is True
    assert sorted(store.list_ids()) == ["instance_1", "instance_2"]


@pytest.mark.usefixtures("unknown_rowcount")
def test_sql_claimed_sequence_hands_out_ids_when_rowcount_is_unknown(tmp_path: Path) -> None:
    """End to end: the allocation returns at once instead of stepping on forever."""

    store = SqlAlchemyIdClaimStore(f"sqlite:///{tmp_path / 'claims.db'}", create_tables=True)
    sequence = _process(1, store.claim)

    assert [sequence.new("instance") for _ in range(3)] == [
        "instance_1",
        "instance_2",
        "instance_3",
    ]
    assert len(store.list_ids()) == 3  # one reservation per id, no runaway rows


@pytest.mark.parametrize(
    ("sequence", "prefix"),
    [
        (ids.INSTANCE_IDS, "instance"),
        (ids.MODEL_IDS, "schema"),
        (ids.MODEL_IDS, "tpl"),
        (ids.ORG_IDS, "org"),
    ],
)
def test_api_claims_every_globally_stored_prefix(sequence: ids.IdSequence, prefix: str) -> None:
    assert sequence.has_claim(prefix)


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
    return str(sid)


def test_an_id_reserved_by_another_process_is_skipped_by_a_real_request() -> None:
    """The next instance number is reserved elsewhere; the new instance takes the one after."""

    sid = _released_schema("Reservierte Kennung")
    reserved = f"instance_{ids.INSTANCE_IDS.peek()}"
    assert api_module._id_claims.claim(reserved)

    created = client.post(f"/schemas/{sid}/instances").json()["id"]

    assert created != reserved
    assert int(created.rsplit("_", 1)[1]) > int(reserved.rsplit("_", 1)[1])
    assert client.get(f"/instances/{reserved}").status_code == 404


def test_admin_reset_keeps_the_reservations() -> None:
    """Another process may hold a reservation it has not saved yet -- reset must not free it."""

    key = "resetprobe_1"  # tiny number: keeping it must not lift any counter
    assert api_module._id_claims.claim(key)

    assert client.post("/admin/reset", json={"load_demo": False}).status_code == 200

    assert api_module._id_claims.claim(key) is False


def test_restart_lifts_the_counters_past_reserved_but_unsaved_ids() -> None:
    """A reservation without a saved object (failed request) still counts as used."""

    sequences = (ids.INSTANCE_IDS, ids.MODEL_IDS, ids.ORG_IDS)
    # Above every stored id: the start-up lift puts all sequences on the
    # highest number of *any* prefix, and node/element ids run far ahead.
    reserved = max(sequence.peek() for sequence in sequences) + 5
    assert api_module._id_claims.claim(f"instance_{reserved}")
    try:
        for sequence in sequences:
            sequence.reset_for_tests(1)  # a freshly started process

        api_module._reserve_stored_ids()

        assert ids.INSTANCE_IDS.peek() == reserved + 1
    finally:
        api_module._reserve_stored_ids()  # never leave later tests at 1


def test_fallback_dialect_treats_only_a_duplicate_key_as_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Databases without ON CONFLICT: plain INSERT, a unique violation means "taken"."""

    store = SqlAlchemyIdClaimStore(f"sqlite:///{tmp_path / 'claims.db'}", create_tables=True)
    monkeypatch.setattr(store._engine.dialect, "name", "otherdb")

    assert store.claim("instance_3") is True
    assert store.claim("instance_3") is False
    assert store.claim("instance_4") is True
    assert sorted(store.list_ids()) == ["instance_3", "instance_4"]


def test_fallback_dialect_reports_other_database_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the fallback path, too, a broken database is an error and never "taken"."""

    store = SqlAlchemyIdClaimStore(f"sqlite:///{tmp_path / 'empty.db'}")  # no tables
    monkeypatch.setattr(store._engine.dialect, "name", "otherdb")

    with pytest.raises(Exception, match="id_claim"):
        store.claim("instance_1")


def test_every_orm_table_has_an_alembic_migration() -> None:
    """A table only ``create_all`` knows would be missing after ``alembic upgrade head``."""

    created: set[str] = set()
    for migration in _MIGRATIONS.glob("*.py"):
        text = migration.read_text(encoding="utf-8")
        created.update(re.findall(r'op\.create_table\(\s*"([a-z_]+)"', text))

    assert set(Base.metadata.tables) <= created, sorted(set(Base.metadata.tables) - created)
