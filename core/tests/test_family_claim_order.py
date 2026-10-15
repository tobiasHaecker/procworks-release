# SPDX-License-Identifier: BUSL-1.1
"""A revision or release reads the family only after claiming its first version.

Revision and release decide by the whole version family (one numbering for
all versions of a process). To keep two concurrent requests on *different*
versions of one family apart, the request saves the family's first version
again (content unchanged, conditionally) in its unit of work: of two such
requests one goes through, the other collides and is repeated on fresh data.

That only works if everything the decision rests on is read **after** the
claim. Read before it, a database with "read committed" transactions lets a
request see the stored schemas as they were before a concurrent request
committed, but the first version as it is after -- the claim then succeeds,
and both requests create (or release) the same version number. Measured with
two API processes against PostgreSQL: two open drafts or two released
schemas of one number.

Pinned here:

* the order itself (claim first, then read the stored schemas), for revision
  and release;
* the race on an SQLite file, forced to the harmful interleaving: one release
  pauses right after reading the sibling draft, the other release of the same
  number runs meanwhile -- exactly one of them succeeds, the other is refused
  with ``LC.version-taken``;
* requests on two *different* families do not hinder each other.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from planted import plant
from sql_backed_api import use_sql_backend
from staffing import staff_via_api, staffed

import procworks.api as api_module
from procworks import new_revision, release
from procworks.api import app

client = TestClient(app)

#: Seconds the paused request waits for the other one. Long enough for an
#: unhindered request to finish; with the fix the other request waits for the
#: claim (SQLite lock), so the pause simply ends after this time.
_PAUSE_SECONDS = 1.5


def _released(name: str) -> str:
    """Create, staff and release a one-step schema over the API; return its id."""

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return str(sid)


def _legacy_family(name: str, sibling_prefix: str) -> tuple[str, str, str]:
    """A family as an old store holds it: two released v2, one v3 draft of each.

    :param name: the schema name.
    :param sibling_prefix: prefix of the planted ids (lets a test decide where
        they sort among the API's ``schema_<n>`` ids).
    :returns: ``(v1, draft_a, draft_b)`` -- the root and the two v3 drafts.
    """

    v1 = _released(name)
    v2a = client.post(f"/schemas/{v1}/revision", json={}).json()["id"]
    staff_via_api(client, v2a)
    assert client.post(f"/schemas/{v2a}/release").status_code == 200
    v1_schema = api_module._store.get(v1)
    assert v1_schema is not None
    sibling = release(staffed(new_revision(v1_schema, new_schema_id=f"{sibling_prefix}-v2b")))
    plant(api_module._store, sibling)
    draft_a = client.post(f"/schemas/{v2a}/revision", json={}).json()["id"]
    staff_via_api(client, draft_a)
    draft_b = staffed(new_revision(sibling, new_schema_id=f"{sibling_prefix}-v3b"))
    plant(api_module._store, draft_b)
    return v1, str(draft_a), draft_b.id


def _record_order(monkeypatch: pytest.MonkeyPatch, root_id: str) -> list[str]:
    """Record when the root is saved and when the stored ids are listed."""

    events: list[str] = []
    store = api_module._store
    original_list = store.list_ids
    original_persist = api_module._persist_schema

    def listed() -> list[str]:
        events.append("list")
        return original_list()

    def persisted(schema: Any) -> Any:
        if schema.id == root_id:
            events.append("claim-root")
        return original_persist(schema)

    monkeypatch.setattr(store, "list_ids", listed)
    monkeypatch.setattr(api_module, "_persist_schema", persisted)
    return events


def test_a_revision_claims_the_family_before_reading_it(monkeypatch: pytest.MonkeyPatch) -> None:
    v1 = _released("Reihenfolge Revision")
    v2 = client.post(f"/schemas/{v1}/revision", json={}).json()["id"]
    staff_via_api(client, v2)
    assert client.post(f"/schemas/{v2}/release").status_code == 200
    events = _record_order(monkeypatch, v1)

    assert client.post(f"/schemas/{v2}/revision", json={}).status_code == 200

    assert events[:2] == ["claim-root", "list"], events


def test_a_release_claims_the_family_before_reading_it(monkeypatch: pytest.MonkeyPatch) -> None:
    v1 = _released("Reihenfolge Freigabe")
    v2 = client.post(f"/schemas/{v1}/revision", json={}).json()["id"]
    staff_via_api(client, v2)
    events = _record_order(monkeypatch, v1)

    assert client.post(f"/schemas/{v2}/release").status_code == 200

    assert events[:2] == ["claim-root", "list"], events


def test_a_first_version_claims_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge: the schema is its own root -- only the request's own save claims it."""

    sid = client.post("/schemas", json={"name": "Erste Version"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    staff_via_api(client, sid)
    events = _record_order(monkeypatch, sid)

    assert client.post(f"/schemas/{sid}/release").status_code == 200

    assert events[0] == "list", events
    assert events.count("claim-root") == 1  # the release itself, after the decision


def _pause_on_first_read(
    monkeypatch: pytest.MonkeyPatch, schema_id: str
) -> tuple[threading.Event, threading.Event]:
    """Pause the first request that loads ``schema_id`` until the other one is done.

    :returns: ``(paused, other_done)`` -- set when the pause begins, and to be
        set by the test when the other request has answered.
    """

    store = api_module._store
    original = store.get
    paused = threading.Event()
    other_done = threading.Event()
    lock = threading.Lock()
    calls = {"n": 0}

    def get(sid: str) -> Any:
        result = original(sid)
        if sid == schema_id:
            with lock:
                calls["n"] += 1
                first = calls["n"] == 1
            if first:
                paused.set()
                other_done.wait(_PAUSE_SECONDS)
        return result

    monkeypatch.setattr(store, "get", get)
    return paused, other_done


def test_two_releases_of_one_number_on_siblings_release_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The harmful interleaving, forced: the sibling is read before the other commits."""

    use_sql_backend(monkeypatch, tmp_path)
    # "a-..." sorts before the API's "schema_<n>": the sibling draft is read
    # before the root, as in the measured race.
    v1, draft_a, draft_b = _legacy_family("Freigabe gleichzeitig", "a-legacy")
    ids = api_module._store.list_ids()
    assert ids.index(draft_b) < ids.index(v1), ids  # precondition of the interleaving
    paused, other_done = _pause_on_first_read(monkeypatch, draft_b)

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(lambda: client.post(f"/schemas/{draft_a}/release"))
        assert paused.wait(5)
        try:
            second: httpx.Response = client.post(f"/schemas/{draft_b}/release")
        finally:
            other_done.set()
        answers = [first.result(), second]

    assert sorted(a.status_code for a in answers) == [200, 422], [a.text for a in answers]
    refused = next(a for a in answers if a.status_code == 422)
    [finding] = refused.json()["detail"]["findings"]
    assert (finding["code"], finding["params"]["version"]) == ("LC.version-taken", "3")
    released = [
        sid
        for sid in (draft_a, draft_b)
        if client.get(f"/schemas/{sid}").json()["lifecycle_state"] == "RELEASED"
    ]
    assert len(released) == 1, released


def test_releases_in_two_families_do_not_hinder_each_other(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Negative: the claim is per family -- another family's release goes through."""

    use_sql_backend(monkeypatch, tmp_path)
    _, draft_one, _ = _legacy_family("Familie eins", "a-one")
    v1_two = _released("Familie zwei")
    draft_two = client.post(f"/schemas/{v1_two}/revision", json={}).json()["id"]
    staff_via_api(client, draft_two)

    with ThreadPoolExecutor(2) as pool:
        futures = [
            pool.submit(lambda: client.post(f"/schemas/{draft_one}/release")),
            pool.submit(lambda: client.post(f"/schemas/{draft_two}/release")),
        ]
        answers = [f.result() for f in futures]

    assert [a.status_code for a in answers] == [200, 200], [a.text for a in answers]
