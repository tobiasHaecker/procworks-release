# SPDX-License-Identifier: BUSL-1.1
"""Reason duties: invisible reasons and the shared refusal, for every test.

Plain whitespace was always refused; these are the cases that passed before:
characters a reader cannot see although they are not whitespace.
"""

from __future__ import annotations

from typing import Any

#: (id, text) pairs: each text consists only of invisible characters.
INVISIBLE_REASONS = [
    ("zero-width-space", "\u200b"),
    ("zero-width-joiners", "\u200b\u200c\u200d"),
    ("word-joiner-and-space", " \u2060 \u200b "),
    ("byte-order-mark", "\ufeff"),
    ("soft-hyphen", "\u00ad"),
    ("hangul-filler", "\u3164"),
    ("braille-blank", "\u2800"),
    ("combining-mark-alone", "\u0301"),
    ("nbsp-and-ideographic-space", "\u00a0\u3000"),
    # Unassigned code points (Cn) show nothing, or an empty box at best ...
    ("unassigned", "\u2065\ufff0"),
    ("unassigned-tag-range", "\U000e0080"),
    # ... and a private-use character (Co) means nothing to the reader.
    ("private-use", "\ue000\U000f0000"),
]


def assert_reason_required(resp: Any, what: str, message: str | None = None) -> None:
    """Assert the shared 422 of a reason duty: ``REQ.reason-required`` for ``what``.

    :param resp: the response of the refused request.
    :param what: the expected ``params.what`` (``fail``, ``supervision``,
        ``automatic``, ``data-correction``, ``adhoc``).
    :param message: the exact ``message`` expected, if the test pins it.
    """

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "REQ.reason-required", detail
    assert detail["params"] == {"what": what}, detail
    if message is not None:
        assert detail["message"] == message
