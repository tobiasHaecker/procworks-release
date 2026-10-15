# SPDX-License-Identifier: BUSL-1.1
"""A released version has at most one open revision.

``new_revision`` set ``version + 1`` without looking at what already existed.
Two revisions of the same v1 could both be released as "version 2"; the
migration target named only one of them, and which one counted was not
recognisable. A second revision is now refused while the first is still a
draft (``LC.open-revision``, naming the draft); a revision of the revision
(v2 → v3) stays possible.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api, staffed

from procworks import LifecycleState, create_empty_schema, new_revision, release, serial_insert
from procworks.api import app
from procworks.validator import CorrectnessError

client = TestClient(app)


def _released_core(schema_id: str):
    schema = create_empty_schema("Revisionen", schema_id=schema_id)
    schema = serial_insert(schema, "A", after_node_id="start")
    return release(staffed(schema))


def test_a_second_open_revision_is_refused_naming_the_first() -> None:
    v1 = _released_core("rev-lib-v1")
    draft = new_revision(v1, new_schema_id="rev-lib-v2a")

    with pytest.raises(CorrectnessError) as exc:
        new_revision(v1, new_schema_id="rev-lib-v2b", existing=[v1, draft])

    [finding] = exc.value.findings
    assert finding.code == "LC.open-revision"
    assert finding.params == {"schema": "rev-lib-v2a", "version": "2"}


def test_unrelated_drafts_do_not_count() -> None:
    v1 = _released_core("rev-lib-u1")
    other = _released_core("rev-lib-u-other")
    other_draft = new_revision(other, new_schema_id="rev-lib-u-other-2")
    revision = new_revision(v1, new_schema_id="rev-lib-u2", existing=[other, other_draft])
    assert revision.version == 2


def test_a_released_revision_is_no_open_revision() -> None:
    """Once v2 is released, revising it (v3) works -- the lineage goes on."""

    v1 = _released_core("rev-lib-r1")
    v2 = release(staffed(new_revision(v1, new_schema_id="rev-lib-r2")))
    v3 = new_revision(v2, new_schema_id="rev-lib-r3", existing=[v1, v2])
    assert (v3.version, v3.revision_of) == (3, "rev-lib-r2")


# --- over the API -------------------------------------------------------------


def _released(name: str) -> str:
    sid = client.post("/schemas", json={"name": name}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    return str(sid)


def test_api_refuses_the_second_revision_and_stores_nothing() -> None:
    v1 = _released("Revision doppelt")
    first = client.post(f"/schemas/{v1}/revision", json={})
    assert first.status_code == 200, first.text
    count = len(client.get("/schemas").json())

    second = client.post(f"/schemas/{v1}/revision", json={})

    assert second.status_code == 422
    [finding] = second.json()["detail"]["findings"]
    assert finding["code"] == "LC.open-revision"
    assert finding["params"] == {"schema": first.json()["id"], "version": "2"}
    assert len(client.get("/schemas").json()) == count


def test_after_release_the_target_is_unique_and_the_next_revision_works() -> None:
    v1 = _released("Revision Kette")
    iid = client.post(f"/schemas/{v1}/instances").json()["id"]
    v2 = client.post(f"/schemas/{v1}/revision", json={}).json()["id"]
    staff_via_api(client, v2)
    assert client.post(f"/schemas/{v2}/release").status_code == 200

    target = client.get(f"/instances/{iid}/migration-target").json()
    assert (target["schema_id"], target["version"]) == (v2, 2)
    v3 = client.post(f"/schemas/{v2}/revision", json={})
    assert v3.status_code == 200, v3.text
    assert v3.json()["version"] == 3


# --- only the newest released version is revised -------------------------------


def test_a_version_with_a_released_successor_is_not_revised_again() -> None:
    """Revising v1 after v2 was released gave a second "version 2" -- and the
    migration target jumped to it. The newest released version is the base."""

    v1 = _released_core("rev-new-1")
    v2 = release(staffed(new_revision(v1, new_schema_id="rev-new-2")))

    with pytest.raises(CorrectnessError) as exc:
        new_revision(v1, new_schema_id="rev-new-2b", existing=[v1, v2])

    [finding] = exc.value.findings
    assert finding.code == "LC.not-latest"
    assert finding.params == {"schema": "rev-new-2", "version": "2"}


def test_the_newest_successor_is_named_also_past_a_withdrawn_one() -> None:
    """v2 released and later deprecated, v3 released: v1 is two versions
    behind; a withdrawn version in between does not reopen it."""

    v1 = _released_core("rev-chain-1")
    v2 = release(staffed(new_revision(v1, new_schema_id="rev-chain-2")))
    v3 = release(staffed(new_revision(v2, new_schema_id="rev-chain-3", existing=[v1, v2])))
    withdrawn = v2.model_copy(update={"lifecycle_state": LifecycleState.DEPRECATED})

    with pytest.raises(CorrectnessError) as exc:
        new_revision(v1, existing=[v1, withdrawn, v3])

    assert [(f.code, f.params) for f in exc.value.findings] == [
        ("LC.not-latest", {"schema": "rev-chain-3", "version": "3"})
    ]


def test_an_open_draft_of_the_successor_does_not_count_as_newer_release() -> None:
    """Only a released (or once released) successor makes a version old; a
    draft revision is the open-revision case and keeps its own code."""

    v1 = _released_core("rev-open-1")
    draft = new_revision(v1, new_schema_id="rev-open-2")

    with pytest.raises(CorrectnessError) as exc:
        new_revision(v1, existing=[v1, draft])

    assert [f.code for f in exc.value.findings] == ["LC.open-revision"]


def test_api_refuses_revising_an_older_version_and_stores_nothing() -> None:
    v1 = _released("Revision veraltet")
    v2 = client.post(f"/schemas/{v1}/revision", json={}).json()["id"]
    staff_via_api(client, v2)
    assert client.post(f"/schemas/{v2}/release").status_code == 200
    count = len(client.get("/schemas").json())

    resp = client.post(f"/schemas/{v1}/revision", json={})

    assert resp.status_code == 422
    [finding] = resp.json()["detail"]["findings"]
    assert (finding["code"], finding["params"]) == (
        "LC.not-latest", {"schema": v2, "version": "2"}
    )
    assert len(client.get("/schemas").json()) == count
    assert client.post(f"/schemas/{v2}/revision", json={}).status_code == 200


# --- duplicates from before the one-revision rule ------------------------------
#
# Stores from before the rule may hold two released "version 2" of the same
# version 1 (siblings). Revising each of them gave two "version 3", and both
# could be released. The checks now look at the whole family (the schemas
# sharing the lineage root), not only at the descendants of the revised
# version. Nothing stored is rewritten: the duplicates stay as they are, they
# only stop multiplying.


def _legacy_family(prefix: str):
    """v1 with two released siblings v2a and v2b (as before the rule)."""

    v1 = _released_core(f"{prefix}-1")
    v2a = release(staffed(new_revision(v1, new_schema_id=f"{prefix}-2a")))
    # The second sibling as an old store holds it: created without the check.
    v2b = release(staffed(new_revision(v1, new_schema_id=f"{prefix}-2b")))
    return v1, v2a, v2b


def test_a_sibling_with_an_open_next_version_blocks_the_revision() -> None:
    v1, v2a, v2b = _legacy_family("dup-open")
    v3 = new_revision(v2a, new_schema_id="dup-open-3", existing=[v1, v2a, v2b])

    with pytest.raises(CorrectnessError) as exc:
        new_revision(v2b, new_schema_id="dup-open-3b", existing=[v1, v2a, v2b, v3])

    assert [(f.code, f.params) for f in exc.value.findings] == [
        ("LC.open-revision", {"schema": "dup-open-3", "version": "3"})
    ]


def test_a_sibling_with_a_released_next_version_makes_the_other_old() -> None:
    v1, v2a, v2b = _legacy_family("dup-rel")
    v3 = release(staffed(new_revision(v2a, new_schema_id="dup-rel-3", existing=[v1, v2a, v2b])))

    with pytest.raises(CorrectnessError) as exc:
        new_revision(v2b, existing=[v1, v2a, v2b, v3])

    assert [(f.code, f.params) for f in exc.value.findings] == [
        ("LC.not-latest", {"schema": "dup-rel-3", "version": "3"})
    ]


def test_one_of_the_duplicates_may_still_be_revised() -> None:
    """Positiv: without a newer or open version, either duplicate is a base."""

    v1, v2a, v2b = _legacy_family("dup-ok")
    v3 = new_revision(v2b, new_schema_id="dup-ok-3", existing=[v1, v2a, v2b])
    assert (v3.version, v3.revision_of) == (3, "dup-ok-2b")


def test_a_version_number_released_in_the_family_is_not_released_twice() -> None:
    """Two v3 drafts from before the check: the second cannot become a second v3."""

    v1, v2a, v2b = _legacy_family("dup-twice")
    draft_a = new_revision(v2a, new_schema_id="dup-twice-3a", existing=[v1, v2a, v2b])
    draft_b = new_revision(v2b, new_schema_id="dup-twice-3b", existing=[v1, v2a, v2b])
    v3 = release(staffed(draft_a), existing=[v1, v2a, v2b, draft_b])

    with pytest.raises(CorrectnessError) as exc:
        release(staffed(draft_b), existing=[v1, v2a, v2b, v3, draft_b])

    assert [(f.code, f.params) for f in exc.value.findings] == [
        ("LC.version-taken", {"schema": "dup-twice-3a", "version": "3"})
    ]


def test_a_withdrawn_version_keeps_its_number() -> None:
    """Grenzfall: a once released, now deprecated v3 still holds the number."""

    v1, v2a, v2b = _legacy_family("dup-dep")
    draft_b = new_revision(v2b, new_schema_id="dup-dep-3b", existing=[v1, v2a, v2b])
    v3 = release(staffed(new_revision(v2a, new_schema_id="dup-dep-3a", existing=[v1, v2a, v2b])))
    withdrawn = v3.model_copy(update={"lifecycle_state": LifecycleState.DEPRECATED})

    with pytest.raises(CorrectnessError) as exc:
        release(staffed(draft_b), existing=[v1, v2a, v2b, withdrawn])

    assert [f.code for f in exc.value.findings] == ["LC.version-taken"]


def test_release_of_an_unrelated_schema_with_the_same_number_is_fine() -> None:
    """Negativ: another family's version 2 does not count."""

    v1 = _released_core("dup-other-1")
    other = _released_core("dup-other-x")
    other_v2 = release(staffed(new_revision(other, new_schema_id="dup-other-x2")))
    released = release(
        staffed(new_revision(v1, new_schema_id="dup-other-2")), existing=[v1, other, other_v2]
    )
    assert (released.version, released.lifecycle_state) == (2, LifecycleState.RELEASED)


def test_api_stops_the_duplicates_from_multiplying_and_keeps_the_store() -> None:
    """Over the API with a planted legacy pair: no second v3, nothing rewritten."""

    import procworks.api as api_module

    v1 = _released("Revision Altbestand")
    v2a = client.post(f"/schemas/{v1}/revision", json={}).json()["id"]
    staff_via_api(client, v2a)
    assert client.post(f"/schemas/{v2a}/release").status_code == 200
    # Plant the second sibling the way an old store holds it.
    v1_schema = api_module._store.get(v1)
    assert v1_schema is not None
    sibling = release(staffed(new_revision(v1_schema, new_schema_id=f"{v1}-alt-2")))
    api_module._store.put(sibling)
    v3 = client.post(f"/schemas/{v2a}/revision", json={})
    assert v3.status_code == 200, v3.text

    second = client.post(f"/schemas/{sibling.id}/revision", json={})
    assert second.status_code == 422
    [finding] = second.json()["detail"]["findings"]
    assert (finding["code"], finding["params"]) == (
        "LC.open-revision", {"schema": v3.json()["id"], "version": "3"}
    )
    # Both version-2 schemas are still there, released and unchanged.
    for sid in (v2a, sibling.id):
        stored = client.get(f"/schemas/{sid}").json()
        assert (stored["version"], stored["lifecycle_state"]) == (2, "RELEASED")


def test_api_refuses_releasing_a_number_twice_and_keeps_the_draft() -> None:
    """Two v3 drafts from before the check: the first release wins, the second is refused."""

    import procworks.api as api_module

    v1 = _released("Freigabe Altbestand")
    v2a = client.post(f"/schemas/{v1}/revision", json={}).json()["id"]
    staff_via_api(client, v2a)
    assert client.post(f"/schemas/{v2a}/release").status_code == 200
    v1_schema = api_module._store.get(v1)
    assert v1_schema is not None
    sibling = release(staffed(new_revision(v1_schema, new_schema_id=f"{v1}-alt-2")))
    api_module._store.put(sibling)
    # Two v3 drafts as an old store holds them (the second without the check).
    draft_a = client.post(f"/schemas/{v2a}/revision", json={}).json()["id"]
    draft_b = new_revision(sibling, new_schema_id=f"{v1}-alt-3")
    api_module._store.put(draft_b)
    staff_via_api(client, draft_a)
    assert client.post(f"/schemas/{draft_a}/release").status_code == 200

    resp = client.post(f"/schemas/{draft_b.id}/release")

    assert resp.status_code == 422
    [finding] = resp.json()["detail"]["findings"]
    assert (finding["code"], finding["params"]) == (
        "LC.version-taken", {"schema": draft_a, "version": "3"}
    )
    assert client.get(f"/schemas/{draft_b.id}").json()["lifecycle_state"] == "ENTWURF"
