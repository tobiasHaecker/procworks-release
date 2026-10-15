# SPDX-License-Identifier: BUSL-1.1
"""``NaN``/``Infinity`` in a request never end in a 500.

The API accepts the JSON literals ``NaN``, ``Infinity`` and ``-Infinity``
(Python's parser does), and FastAPI's 422 for an invalid field echoes the
input. The response encoder refuses non-finite floats, so the *answer* to an
invalid request broke with a 500 while it was being written. The validation
handler now spells them as strings; the business rules that refuse a
non-finite value with their own code (``T1.*-not-finite``,
``K7.threshold-not-finite``, ``U2.bounds-not-finite`` ...) are untouched.

The sweep goes over every operation of the OpenAPI document that takes a JSON
body and puts each literal into each numeric property of that body.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from procworks.api import app

client = TestClient(app, raise_server_exceptions=False)

LITERALS = ("NaN", "Infinity", "-Infinity")


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Follow a ``$ref`` into ``components/schemas``."""

    ref = schema.get("$ref")
    if ref:
        return dict(spec["components"]["schemas"][ref.rsplit("/", 1)[1]])
    return schema


def _is_numeric(spec: dict[str, Any], prop: dict[str, Any]) -> bool:
    """Whether a property is an integer/number (also as ``T | None``)."""

    prop = _resolve(spec, prop)
    if prop.get("type") in ("integer", "number"):
        return True
    return any(_is_numeric(spec, option) for option in prop.get("anyOf", []))


def _numeric_body_fields() -> list[tuple[str, str, str]]:
    """``(method, path, property)`` for every numeric top-level body property."""

    spec = app.openapi()
    found: list[tuple[str, str, str]] = []
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            content = operation.get("requestBody", {}).get("content", {})
            body = content.get("application/json", {}).get("schema")
            if not body:
                continue
            body = _resolve(spec, body)
            for name, prop in body.get("properties", {}).items():
                if _is_numeric(spec, prop):
                    found.append((method, path, name))
    return found


FIELDS = _numeric_body_fields()


def test_the_sweep_finds_the_known_numeric_fields() -> None:
    """Guard of the sweep itself: it must see the fields of the report."""

    assert ("post", "/v1/external-tasks/fetch-and-lock", "lock_ms") in FIELDS
    assert ("post", "/v1/external-tasks/fetch-and-lock", "max_tasks") in FIELDS
    assert ("post", "/v1/connectors/{connector_id}/sample-read", "limit") in FIELDS
    assert len(FIELDS) >= 10


@pytest.mark.parametrize("literal", LITERALS)
@pytest.mark.parametrize(("method", "path", "field"), FIELDS)
def test_a_non_finite_number_never_yields_a_server_error(
    method: str, path: str, field: str, literal: str
) -> None:
    url = path.replace("{", "").replace("}", "")  # dummy ids: schema_id -> "schema_id"
    body = '{"' + field + '": ' + literal + "}"
    res = client.request(
        method.upper(), url, content=body, headers={"Content-Type": "application/json"}
    )
    assert res.status_code < 500, (url, field, literal, res.status_code, res.text[:200])
    json.loads(res.text, parse_constant=_no_constants)  # strict JSON


@pytest.mark.parametrize("literal", LITERALS)
@pytest.mark.parametrize("field", ["lock_ms", "max_tasks"])
def test_fetch_and_lock_answers_422_with_the_literal_as_text(field: str, literal: str) -> None:
    body = '{"worker_id": "w", "topics": ["x"], "' + field + '": ' + literal + "}"
    res = client.post(
        "/v1/external-tasks/fetch-and-lock",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 422, res.text
    detail = json.loads(res.text, parse_constant=_no_constants)["detail"]
    errors = [e for e in detail if e["loc"] == ["body", field]]
    assert errors and errors[0]["input"] == literal


def test_sample_read_with_a_non_finite_limit_is_422() -> None:
    res = client.post(
        "/v1/connectors/erp/sample-read",
        content='{"entity": "Kunde", "limit": NaN}',
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 422
    detail = json.loads(res.text, parse_constant=_no_constants)["detail"]
    assert [e["loc"] for e in detail] == [["body", "limit"]]


def test_other_validation_errors_keep_their_shape() -> None:
    res = client.post("/v1/external-tasks/fetch-and-lock", json={"worker_id": "w"})
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert detail[0]["type"] == "missing"
    assert detail[0]["loc"] == ["body", "topics"]
    assert set(detail[0]) >= {"type", "loc", "msg", "input"}


def _no_constants(name: str) -> object:
    """``parse_constant`` hook: fail on ``NaN``/``Infinity`` -- strict JSON."""

    raise AssertionError(f"non-finite literal {name} in a response")


@pytest.mark.parametrize("literal", LITERALS)
def test_a_trusted_timestamp_must_be_finite(literal: str) -> None:
    """A non-finite "trusted" time would be anchored into audit and ratchet."""

    res = client.post(
        "/license/refresh-time",
        content='{"trusted_now": ' + literal + "}",
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 422, res.text
    detail = json.loads(res.text, parse_constant=_no_constants)["detail"]
    assert [(e["loc"], e["type"]) for e in detail] == [(["body", "trusted_now"], "finite_number")]
