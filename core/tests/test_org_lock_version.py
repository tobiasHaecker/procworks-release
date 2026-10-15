# SPDX-License-Identifier: BUSL-1.1
"""Shared organisations are saved conditionally, like cases and models.

A change of a shared organisation loads it, changes a copy and saves it.
Without a version, two requests that loaded the same state both saved, and
the later save replaced the earlier one: both callers got 200, one change
was gone. A referencing schema hid that (it is saved again in the same unit
and conflicted), so the loss showed exactly where no schema used the
organisation yet -- every freshly created one.

Pinned here, against the in-memory stores and SQLAlchemy stores on SQLite:

* the store contract: a stale save is refused (``ConcurrentModification``
  with kind ``org``), a new organisation never replaces a stored one, a
  loaded copy is detached from the store;
* two concurrent changes of one organisation without a referencing schema
  both survive (the second is repeated on the new state);
* a conflict that persists answers ``409 DB.conflict`` with ``kind`` ``org``
  and stores nothing;
* creating an organisation under an id that is taken is refused with
  ``OP.already-exists`` instead of replacing it.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sql_backed_api import use_sql_backend
from sqlalchemy import create_engine, text
from web_vm import needs_node, run_app_js

import procworks.api as api_module
from procworks import api_org
from procworks.api import app
from procworks.db import SqlAlchemyOrgStore
from procworks.org import create_org_model, org_add_role
from procworks.store import InMemoryOrgStore
from procworks.unit_of_work import ConcurrentModification

client = TestClient(app)

_BARRIER_TIMEOUT = 0.5


@pytest.fixture(params=["memory", "sqlite"])
def org_store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "memory":
        return InMemoryOrgStore()
    return SqlAlchemyOrgStore(f"sqlite:///{tmp_path / 'org.db'}", create_tables=True)


@pytest.fixture(params=["memory", "sqlite"])
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    if request.param == "sqlite":
        use_sql_backend(monkeypatch, tmp_path)
    return str(request.param)


# --- the store contract --------------------------------------------------------------


def test_a_stale_save_of_an_org_is_refused_and_the_first_survives(org_store: Any) -> None:
    org_store.put(create_org_model("Amt", org_id="org-store"))
    first = org_store.get("org-store")
    second = org_store.get("org-store")

    org_store.put(org_add_role(first, "Sachbearbeitung", role_id="sb"))
    with pytest.raises(ConcurrentModification) as refused:
        org_store.put(org_add_role(second, "Leitung", role_id="lt"))

    assert (refused.value.kind, refused.value.key) == ("org", "org-store")
    assert set(org_store.get("org-store").roles) == {"sb"}


def test_a_saved_org_can_be_saved_again_by_the_same_request(org_store: Any) -> None:
    org_store.put(create_org_model("Amt", org_id="org-again"))
    org = org_store.get("org-again")
    org = org_store.put(org_add_role(org, "A", role_id="a"))

    org_store.put(org_add_role(org, "B", role_id="b"))

    assert set(org_store.get("org-again").roles) == {"a", "b"}


def test_a_new_org_never_replaces_a_stored_one(org_store: Any) -> None:
    org_store.put(org_add_role(create_org_model("Alt", org_id="org-new"), "A", role_id="a"))

    with pytest.raises(ConcurrentModification) as refused:
        org_store.put(create_org_model("Neu", org_id="org-new"))

    assert refused.value.kind == "org"
    assert org_store.get("org-new").name == "Alt"


def test_a_loaded_org_is_detached_from_the_store(org_store: Any) -> None:
    org_store.put(create_org_model("Amt", org_id="org-copy"))
    loaded = org_store.get("org-copy")

    loaded.name = "geändert, nicht gespeichert"

    assert org_store.get("org-copy").name == "Amt"


# --- the request paths -----------------------------------------------------------------


def _gate(monkeypatch: pytest.MonkeyPatch, module: object, name: str) -> None:
    """Hold the first two calls of ``module.name`` until both arrived (after the load)."""

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
    with ThreadPoolExecutor(len(calls)) as pool:
        return [f.result() for f in [pool.submit(call) for call in calls]]


def test_two_changes_of_an_unreferenced_org_both_survive(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id = f"org-zwei-{backend}"
    created = client.post("/org-models", json={"name": "Amt", "org_model_id": org_id})
    assert created.status_code == 201
    _gate(monkeypatch, api_org.org_ops, "org_add_role")

    answers = _race(
        lambda: client.post(f"/org-models/{org_id}/roles", json={"name": "A", "role_id": "ra"}),
        lambda: client.post(f"/org-models/{org_id}/roles", json={"name": "B", "role_id": "rb"}),
    )

    assert [a.status_code for a in answers] == [200, 200], [a.text for a in answers]
    assert set(client.get(f"/org-models/{org_id}").json()["roles"]) == {"ra", "rb"}


def test_a_persisting_conflict_answers_409_with_kind_org(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id = f"org-konflikt-{backend}"
    client.post("/org-models", json={"name": "Amt", "org_model_id": org_id})
    store = api_module._org_store
    real_put = store.put

    def always_stale(org: Any) -> Any:
        if org.id == org_id:
            raise api_module.unit_of_work.note_conflict(ConcurrentModification("org", org_id))
        return real_put(org)

    monkeypatch.setattr(store, "put", always_stale)

    resp = client.post(f"/org-models/{org_id}/roles", json={"name": "A", "role_id": "ra"})

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "DB.conflict"
    assert resp.json()["detail"]["params"] == {"kind": "org", "id": org_id}
    monkeypatch.setattr(store, "put", real_put)
    assert client.get(f"/org-models/{org_id}").json()["roles"] == {}


def test_creating_an_org_under_a_taken_id_is_refused(backend: str) -> None:
    org_id = f"org-belegt-{backend}"
    client.post("/org-models", json={"name": "Erste", "org_model_id": org_id})
    client.post(f"/org-models/{org_id}/roles", json={"name": "A", "role_id": "ra"})

    resp = client.post("/org-models", json={"name": "Zweite", "org_model_id": org_id})

    assert resp.status_code == 422
    (finding,) = resp.json()["detail"]["findings"]
    assert finding["code"] == "OP.already-exists"
    assert finding["params"] == {"kind": "org_model", "name": org_id}
    kept = client.get(f"/org-models/{org_id}").json()
    assert (kept["name"], set(kept["roles"])) == ("Erste", {"ra"})


@needs_node
def test_the_client_names_the_organisation_in_the_conflict(tmp_path: Path) -> None:
    title = run_app_js(
        "return describeError({ status: 409, detail: { message: 'x', code: 'DB.conflict', "
        "params: { kind: 'org', id: 'org_1' } } }).title;",
        tmp_path,
    )
    assert title.startswith("Die Organisation wurde inzwischen von jemand anderem geändert")


# --- the migration -----------------------------------------------------------------------


def test_the_migration_starts_stored_orgs_at_version_one_and_the_downgrade_keeps_them(
    tmp_path: Path,
) -> None:
    pytest.importorskip("alembic")
    core = Path(__file__).resolve().parents[1]
    url = f"sqlite:///{tmp_path / 'org.db'}"

    def alembic(*args: str) -> None:
        env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(core / "src")}
        done = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=core, env=env,
            capture_output=True, text=True, timeout=120, check=False,
        )
        assert done.returncode == 0, done.stderr

    def rows(query: str) -> list[tuple[Any, ...]]:
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                return [tuple(r) for r in conn.execute(text(query))]
        finally:
            engine.dispose()

    alembic("upgrade", "0017_audit_history_record")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO org_model (id, name, document) VALUES ('org-alt', 'Alt', :doc)"),
            {"doc": '{"id": "org-alt", "name": "Alt", "roles": {}, "org_units": {}, "agents": {}}'},
        )
    engine.dispose()

    alembic("upgrade", "0018_org_lock_version")

    assert rows("SELECT id, lock_version FROM org_model") == [("org-alt", 1)]
    store = SqlAlchemyOrgStore(url)
    org = store.get("org-alt")
    store.put(org_add_role(org, "A", role_id="a"))
    assert rows("SELECT lock_version FROM org_model") == [(2,)]

    alembic("downgrade", "0017_audit_history_record")

    assert [r[1] for r in rows("PRAGMA table_info(org_model)")] == ["id", "name", "document"]
    assert rows("SELECT id, name FROM org_model") == [("org-alt", "Alt")]
