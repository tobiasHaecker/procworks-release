# SPDX-License-Identifier: BUSL-1.1
"""A JSON body that is not valid UTF-8 is refused at the boundary (422).

JSON travels as UTF-8. The parser behind the routes decodes a byte body
leniently, though: it lets the UTF-8 *encoding* of a surrogate (bytes
``ED A0 80`` for U+D800, or a pair encoded that way, so-called CESU-8)
through as a lone surrogate -- a string no UTF-8 encoder can write again,
neither the response encoder nor PostgreSQL. Such a request ended in 500,
on creating a process as on completing a step or setting instance data.
Other invalid byte sequences (a stray ``FF``, a cut-off or overlong
sequence) ended in a generic 400 without a code.

Every JSON body is therefore decoded strictly as UTF-8 before any route
runs; if that fails, the answer is 422 with ``detail.code``
``REQ.invalid-encoding`` and ``params.where`` ``body`` -- nothing is
executed. Valid UTF-8 passes unchanged: four-byte characters (emoji), the
escaped surrogate pair, a leading byte order mark.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from web_source import app_js_source

from procworks.api import _store, app

client = TestClient(app, raise_server_exceptions=False)

#: U+1F600 written as raw UTF-8 (four bytes) and as an escaped JSON pair.
_EMOJI_UTF8 = b"\xf0\x9f\x98\x80"
_EMOJI_ESCAPED = b"\\" + b"ud83d" + b"\\" + b"ude00"


def _send(method: str, path: str, raw: bytes):  # type: ignore[no-untyped-def]
    return client.request(method, path, content=raw,
                           headers={"content-type": "application/json"})


def _name(inner: bytes) -> bytes:
    return b'{"name": "A' + inner + b'B"}'


@pytest.mark.parametrize(
    "raw",
    [
        _name(b"\xed\xa0\x80"),  # high surrogate U+D800, encoded as UTF-8
        _name(b"\xed\xb0\x80"),  # low surrogate U+DC00
        _name(b"\xed\xa0\xbd\xed\xb8\x80"),  # pair encoded half by half (CESU-8)
        _name(b"\xed\xbf\xbf"),  # last surrogate U+DFFF
        _name(b"\xff"),  # a byte that never occurs in UTF-8
        _name(b"\xc0\xaf"),  # overlong "/" (two bytes)
        _name(b"\xe0\x80\xaf"),  # overlong "/" (three bytes)
        _name(b"\xf0\x82\x82\xac"),  # overlong euro sign (four bytes)
        _name(b"\xf4\x90\x80\x80"),  # above U+10FFFF
        _name(b"\xf0\x9f\x98"),  # emoji cut off inside the string
        _name(b"\xe2\x82"),  # euro sign cut off
        _name(b"\x80"),  # continuation byte without a start
        b'{"name": "ok"}\xed\xa0\x80',  # surrogate as the very last bytes
        b'{"name": "ok"}\xf0\x9f\x98',  # cut-off sequence at the end of the body
        b'{"name": "\xed\xa0\x80',  # surrogate at the end of a truncated body
    ],
)
def test_invalid_utf8_is_refused_with_its_code_and_nothing_is_stored(raw: bytes) -> None:
    count = len(_store.list_ids())
    res = _send("POST", "/schemas", raw)
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "REQ.invalid-encoding"
    assert detail["params"] == {"where": "body"}
    assert "UTF-8" in detail["message"]
    assert len(_store.list_ids()) == count
    assert client.get("/schemas").status_code == 200  # the next request is served


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/instances/no-such-instance/data"),
        ("POST", "/instances/no-such-instance/complete"),
        ("PUT", "/v1/instances/no-such-instance/data"),
    ],
)
def test_the_check_runs_before_any_route_on_every_data_path(method: str, path: str) -> None:
    """Refused before routing: the instance does not even exist, yet the
    answer is the encoding refusal, not 404."""

    raw = b'{"values": {"x": "\xed\xa0\x80"}}'
    res = _send(method, path, raw)
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == "REQ.invalid-encoding"


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        (_name(_EMOJI_UTF8), "A\U0001f600B"),  # four-byte UTF-8
        (_name(_EMOJI_ESCAPED), "A\U0001f600B"),  # escaped pair
        (_name("ä€퟿\U0010ffff".encode()), "Aä€퟿\U0010ffffB"),
        (b"\xef\xbb\xbf" + _name(b"x"), "AxB"),  # leading byte order mark
    ],
    ids=["emoji-utf8", "emoji-escaped", "edges-around-surrogates", "bom"],
)
def test_valid_utf8_passes_unchanged(raw: bytes, name: str) -> None:
    res = _send("POST", "/schemas", raw)
    assert res.status_code == 201, res.text
    assert res.json()["name"] == name


@pytest.mark.parametrize("raw", [b"\xed\xa0\x80", b"\xff", b"A\xe2\x82"])
def test_a_body_of_another_type_keeps_the_validation_answer_without_a_500(raw: bytes) -> None:
    """A non-JSON body is not inspected by the boundary (uploads may be
    binary). A route that expects JSON answers it with the usual validation
    422, which echoes the body -- decoded with replacement characters, so an
    invalid byte no longer turns that 422 into a 500."""

    res = client.post("/schemas", content=raw, headers={"content-type": "text/plain"})
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert isinstance(detail, list)
    assert detail[0]["loc"] == ["body"]
    assert "\ufffd" in detail[0]["input"]


def test_the_web_client_words_the_refusal() -> None:
    """The boundary codes are constants, not ``code="…"`` literals, so the
    general catalog guard does not see them -- this one does."""

    catalog = app_js_source()
    for code in ("REQ.invalid-encoding", "REQ.lone-surrogate", "REQ.nul-character"):
        assert f'  "{code}": ' in catalog, code
