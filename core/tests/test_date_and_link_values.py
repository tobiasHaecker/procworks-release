# SPDX-License-Identifier: BUSL-1.1
"""DATE and URI values are type-checked like every other type (runtime D3).

``value_matches_type`` took any string for DATE and URI. ``"2026-02-30"``,
``"hallo"`` or ``"kein uri"`` were stored; a DATE filter of a SQL binding then
failed only inside the database, and a link field held free text. Now a DATE
is a real calendar date written ``YYYY-MM-DD`` and a URI an ``http``,
``https`` or ``mailto`` link -- on every inbound path (data write, completion
over ``/api`` and ``/v1``, migration start values, simulation seed), with the
same coded finding ``D3.wrong-type``. Values stored before the check stay
readable: nothing re-validates on read.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from staffing import staff_via_api

from procworks import api as api_module
from procworks.model import DataType, value_matches_type

client = TestClient(api_module.app)


# --- the type check itself ---------------------------------------------------


@pytest.mark.parametrize("value", ["2024-02-29", "2026-10-10", "0001-01-01", "9999-12-31"])
def test_real_calendar_dates_are_dates(value: str) -> None:
    assert value_matches_type(DataType.DATE, value)


@pytest.mark.parametrize(
    "value",
    [
        "2026-02-30",  # no such day
        "2023-02-29",  # not a leap year
        "2026-13-01",
        "hallo",
        "30.02.2026",
        "28.02.2026",  # a German spelling of a real date is still not the format
        "20260228",  # ISO basic form, accepted by date.fromisoformat
        "2026-W01-1",  # ISO week date, ditto
        "2026-02-28T10:00:00",  # a timestamp is not a date
        " 2026-02-28",
        "",
        20260228,
        None,
    ],
)
def test_anything_else_is_not_a_date(value: object) -> None:
    assert not value_matches_type(DataType.DATE, value)


@pytest.mark.parametrize(
    "value",
    [
        "https://example.org/x",
        "http://example.org",
        "HTTPS://Example.org/Pfad?q=1#frag",
        "mailto:anna@example.org",
        "https://[2001:db8::1]/x",
    ],
)
def test_web_and_mail_links_are_uris(value: str) -> None:
    assert value_matches_type(DataType.URI, value)


@pytest.mark.parametrize(
    "value",
    [
        "kein uri",
        "example.org",
        "javascript:alert(1)",
        "file:///etc/passwd",
        "ftp://example.org/x",
        "data:text/html,hi",
        "https://",
        "https:///nur-pfad",
        "mailto:",
        "https://exa mple.org",
        "https://example.org/\nx",
        "https://[::1",
        "",
        42,
    ],
)
def test_anything_else_is_not_a_uri(value: object) -> None:
    assert not value_matches_type(DataType.URI, value)


def test_strings_stay_free_text() -> None:
    assert value_matches_type(DataType.STRING, "2026-02-30")
    assert value_matches_type(DataType.STRING, "kein uri")
    assert value_matches_type(DataType.STRING, "")


# --- every inbound path ------------------------------------------------------


def _released(name: str) -> tuple[str, str, str]:
    """Released process: Erfassen writes DATE 'dt' and URI 'u' (both optional).

    Returns the schema id, the id of 'Erfassen' and a fresh instance id.
    """

    sid = client.post("/schemas", json={"name": name}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    for element_id, data_type in (("dt", "DATE"), ("u", "URI")):
        created = client.post(
            f"/schemas/{sid}/data-elements",
            json={"name": element_id, "data_type": data_type, "element_id": element_id},
        )
        assert created.status_code == 200, created.text
        bound = client.post(
            f"/schemas/{sid}/data-access",
            json={"node_id": act, "element_id": element_id, "mode": "WRITE", "mandatory": False},
        )
        assert bound.status_code == 200, bound.text
    staff_via_api(client, sid)
    assert client.post(f"/schemas/{sid}/release").status_code == 200
    iid = client.post(f"/schemas/{sid}/instances").json()["id"]
    return sid, act, iid


def _codes(resp: Any) -> list[tuple[str, str]]:
    findings = resp.json()["detail"]["findings"]
    return [(f["code"], f["params"]["type"]) for f in findings]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"dt": "2026-02-30"}, [("D3.wrong-type", "DATE")]),
        ({"dt": "30.02.2026"}, [("D3.wrong-type", "DATE")]),
        ({"u": "kein uri"}, [("D3.wrong-type", "URI")]),
    ],
    ids=["impossible-day", "german-notation", "free-text-link"],
)
def test_data_write_refuses_a_malformed_date_or_link(
    values: dict[str, object], expected: list[tuple[str, str]]
) -> None:
    _sid, _act, iid = _released("Datum Daten")
    resp = client.put(f"/instances/{iid}/data", json={"values": values})
    assert resp.status_code == 422
    assert _codes(resp) == expected
    assert client.get(f"/instances/{iid}").json()["data_values"] == {}


def test_completion_refuses_both_and_stores_nothing() -> None:
    _sid, act, iid = _released("Datum Abschluss")
    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act, "data": {"dt": "hallo", "u": "kein uri"}},
    )
    assert resp.status_code == 422
    assert sorted(_codes(resp)) == [("D3.wrong-type", "DATE"), ("D3.wrong-type", "URI")]
    instance = client.get(f"/instances/{iid}").json()
    assert instance["data_values"] == {}
    assert instance["node_states"][act] in ("ACTIVATED", "RUNNING")


def test_v1_completion_refuses_a_non_leap_day() -> None:
    _sid, act, iid = _released("Datum v1")
    resp = client.post(
        f"/v1/instances/{iid}/nodes/{act}/complete", json={"data": {"dt": "2023-02-29"}}
    )
    assert resp.status_code == 422
    assert _codes(resp) == [("D3.wrong-type", "DATE")]


def test_valid_date_and_link_are_taken_over() -> None:
    _sid, act, iid = _released("Datum gut")
    resp = client.post(
        f"/instances/{iid}/complete",
        json={"node_id": act, "data": {"dt": "2024-02-29", "u": "https://example.org/x"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data_values"] == {"dt": "2024-02-29", "u": "https://example.org/x"}


def test_migration_start_value_is_checked() -> None:
    sid, _act, iid = _released("Datum Migration")
    revision = client.post(f"/schemas/{sid}/revision", json={})
    assert revision.status_code == 200, revision.text
    target = revision.json()["id"]
    staff_via_api(client, target)
    assert client.post(f"/schemas/{target}/release").status_code == 200
    resp = client.post(
        f"/instances/{iid}/migrate",
        json={"target_schema_id": target, "data_mapping": {"dt": "2026-02-30"}},
    )
    assert resp.status_code == 422, resp.text
    assert _codes(resp) == [("D3.wrong-type", "DATE")]


def test_simulation_seed_is_checked() -> None:
    sid = client.post("/schemas", json={"name": "Datum Simulation"}).json()["id"]
    schema = client.post(
        f"/schemas/{sid}/serial-insert", json={"label": "Erfassen", "after_node_id": "start"}
    ).json()
    act = next(n["id"] for n in schema["nodes"].values() if n["label"] == "Erfassen")
    client.post(
        f"/schemas/{sid}/data-elements",
        json={"name": "dt", "data_type": "DATE", "element_id": "dt"},
    )
    client.post(
        f"/schemas/{sid}/data-access",
        json={"node_id": act, "element_id": "dt", "mode": "WRITE", "mandatory": False},
    )
    resp = client.post(f"/schemas/{sid}/simulate", json={"data": {"dt": "hallo"}})
    assert resp.status_code == 422, resp.text
    assert _codes(resp) == [("D3.wrong-type", "DATE")]


def test_a_value_stored_before_the_check_stays_readable() -> None:
    """Old free-text dates are not re-validated: the case loads and lists."""

    _sid, act, iid = _released("Datum Altbestand")
    stored = api_module._instances.get(iid)
    assert stored is not None
    legacy = stored.model_copy(deep=True)
    legacy.data_values.update({"dt": "30.02.2026", "u": "kein uri"})
    api_module._instances.put(legacy)

    assert client.get(f"/instances/{iid}").status_code == 200
    assert client.get(f"/instances/{iid}/tasks").status_code == 200
    assert client.get(f"/instances/{iid}/audit").status_code == 200
    # Completing without touching the old values still works.
    done = client.post(f"/instances/{iid}/complete", json={"node_id": act, "data": {}})
    assert done.status_code == 200, done.text
    assert done.json()["data_values"]["dt"] == "30.02.2026"
