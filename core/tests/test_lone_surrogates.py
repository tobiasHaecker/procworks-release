# SPDX-License-Identifier: BUSL-1.1
"""A lone UTF-16 surrogate in a JSON body is refused at the boundary (422).

JSON allows the escape ``\\ud800`` on its own, Python's parser turns it into a
string that cannot be encoded as UTF-8 -- neither by the response encoder nor
by PostgreSQL. A name with such a character was stored and then answered
with 500 (the change was already written). The API now refuses the body
before any route runs (``REQ.lone-surrogate``); a proper pair (an emoji
written as ``\\ud83d\\ude00``) and the *text* ``\\ud800`` stay allowed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from procworks.api import _has_lone_surrogate, _store, app

client = TestClient(app, raise_server_exceptions=False)

#: The two halves of U+1F600 as JSON escapes, built from parts so no tool on
#: the way can fold the pair into the character itself.
HIGH = b"\\" + b"ud83d"
LOW = b"\\" + b"ude00"
_PAIR = b'{"name": "' + HIGH + LOW + b'"}'
_PAIR_TOO_MANY = b'{"name": "' + HIGH + HIGH + LOW + b'"}'


def _post(path: str, raw: bytes):  # type: ignore[no-untyped-def]
    return client.post(path, content=raw, headers={"content-type": "application/json"})


@pytest.mark.parametrize(
    "raw",
    [
        rb'{"name": "A\ud800B"}',  # high surrogate alone
        rb'{"name": "\udc00"}',  # low surrogate alone
        rb'{"name": "\ud83d"}',  # high surrogate at the end of the string
        rb'{"name": "\ude00\ud83d"}',  # pair in the wrong order
        _PAIR_TOO_MANY,  # one high too many
        rb'{"name": "\\\ud800"}',  # escaped backslash, then a real escape
        rb'{"name": "ok", "x": ["\uDBFF"]}',  # upper case, nested
    ],
)
def test_a_lone_surrogate_is_refused_and_nothing_is_stored(raw: bytes) -> None:
    count = len(_store.list_ids())
    res = _post("/schemas", raw)
    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "REQ.lone-surrogate"
    assert detail["params"] == {"where": "body"}
    assert len(_store.list_ids()) == count


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        (_PAIR, "\U0001f600"),  # a proper pair
        (rb'{"name": "\\ud800"}', "\\ud800"),  # the text, not the escape
        (rb'{"name": "\u00e4\u20ac"}', "\u00e4\u20ac"),  # ordinary escapes
    ],
)
def test_proper_pairs_and_plain_text_pass(raw: bytes, name: str) -> None:
    res = _post("/schemas", raw)
    assert res.status_code == 201, res.text
    assert res.json()["name"] == name


@pytest.mark.parametrize(
    ("raw", "lone"),
    [
        (rb"\ud800", True),
        (HIGH + LOW, False),
        (rb"\\ud800", False),
        (rb"\\\ud800", True),
        (rb"\ud800\\udc00", True),
        (rb"\udfff", True),
        (rb"\uE000", False),
        (rb"", False),
    ],
)
def test_the_scan_pairs_escapes_like_a_json_parser(raw: bytes, lone: bool) -> None:
    assert _has_lone_surrogate(raw) is lone
