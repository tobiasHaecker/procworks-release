# SPDX-License-Identifier: BUSL-1.1
"""U+0000 (NUL) is refused at the boundary, with 422 and a code.

PostgreSQL cannot store NUL in a text or JSONB value. A name or label with NUL
passed every check and failed only when the store wrote it: 500, and behind
the reverse proxy the next request on the same upstream connection got a 502.
The API now refuses NUL in the path, the query string and a JSON body before
any route runs (``REQ.nul-character``); every other character stays allowed.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from procworks.api import _store, app

client = TestClient(app)

NUL = "\u0000"


def _assert_refused(res, where: str) -> None:  # type: ignore[no-untyped-def]
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "REQ.nul-character"
    assert detail["params"] == {"where": where}


def _schema_id() -> str:
    res = client.post("/schemas", json={"name": "NUL-Test"})
    assert res.status_code == 201
    return str(res.json()["id"])


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/schemas", {"name": f"A{NUL}B"}),
        ("/schemas/{sid}/serial-insert", {"label": f"A{NUL}B", "after_node_id": "start"}),
        ("/schemas/{sid}/data-elements", {"name": f"A{NUL}B", "data_type": "STRING"}),
        ("/schemas/{sid}/roles", {"name": f"A{NUL}B"}),
        ("/org-models", {"name": f"A{NUL}B"}),
        ("/users", {"username": f"a{NUL}b", "password": "x" * 12, "roles": ["viewer"]}),
        ("/templates", {"schema_id": "{sid}", "name": f"A{NUL}B"}),
    ],
)
def test_nul_in_a_name_is_refused_and_nothing_changes(path: str, body: dict[str, object]) -> None:
    sid = _schema_id()
    before = _store.get(sid).model_dump()
    count = len(_store.list_ids())
    payload = json.loads(json.dumps(body).replace("{sid}", sid))

    res = client.post(path.replace("{sid}", sid), json=payload)

    _assert_refused(res, "body")
    assert _store.get(sid).model_dump() == before
    assert len(_store.list_ids()) == count


def test_nul_deep_inside_a_body_is_refused() -> None:
    sid = _schema_id()
    res = client.post(
        f"/schemas/{sid}/serial-insert",
        json={"label": "ok", "after_node_id": "start", "extra": {"list": ["x", f"y{NUL}"]}},
    )
    _assert_refused(res, "body")


def test_a_raw_nul_byte_in_a_json_body_is_refused() -> None:
    res = client.post(
        "/schemas",
        content=b'{"name": "A\x00B"}',
        headers={"Content-Type": "application/json"},
    )
    _assert_refused(res, "body")


def test_a_body_without_content_type_is_checked_too() -> None:
    """FastAPI reads such a body as JSON as well."""

    res = client.post("/schemas", content=b'{"name": "A\\u0000B"}')
    _assert_refused(res, "body")


@pytest.mark.parametrize(
    "path",
    [
        "/schemas/%00",
        "/instances/%00",
        "/instances/%00/audit",
        "/templates/%00",
        "/org-models/%00",
        "/schemas/ab%00cd/bpmn",
    ],
)
def test_nul_in_a_path_is_refused(path: str) -> None:
    _assert_refused(client.get(path), "path")


def test_nul_in_a_query_is_refused() -> None:
    _assert_refused(client.get("/v1/connectors/x/columns?entity=a%00b"), "query")


def test_the_next_request_gets_its_normal_answer() -> None:
    for _ in range(10):
        _assert_refused(client.post("/schemas", json={"name": f"A{NUL}"}), "body")
        assert client.get("/schemas").status_code == 200


@pytest.mark.parametrize(
    "name",
    [
        "Prüfung Größe ß",
        "Freigabe ✅ 🚀",
        "שלום עולם",  # right-to-left
        "مرحبا",
        "A\\u0000B",  # the *text* backslash-u-0000, not a NUL character
        "A\\\\u0000",
    ],
)
def test_other_characters_stay_allowed(name: str) -> None:
    res = client.post("/schemas", json={"name": name})
    assert res.status_code == 201, res.text
    assert res.json()["name"] == name


def test_an_escaped_backslash_before_a_nul_is_still_a_nul() -> None:
    """``\\\\\\u0000`` in JSON is a backslash followed by NUL."""

    res = client.post(
        "/schemas",
        content=b'{"name": "A\\\\\\u0000"}',
        headers={"Content-Type": "application/json"},
    )
    _assert_refused(res, "body")


def test_a_non_json_body_is_not_inspected() -> None:
    """Uploads are not text values; the route decides about them (here: 422
    from body validation, not the NUL refusal)."""

    res = client.post(
        "/schemas", content=b"A\x00B", headers={"Content-Type": "application/octet-stream"}
    )
    assert res.status_code == 422
    assert isinstance(res.json()["detail"], list)  # pydantic errors, no NUL refusal


def test_the_refusal_carries_cors_headers() -> None:
    res = client.post(
        "/schemas",
        json={"name": f"A{NUL}"},
        headers={"Origin": "http://localhost:5500"},
    )
    assert res.status_code == 422
    assert "access-control-allow-origin" in res.headers
