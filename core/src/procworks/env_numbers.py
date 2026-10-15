# SPDX-License-Identifier: BUSL-1.1
"""Strict reading of numbers from environment variables.

Python's own parsers accept far more than an operator means to write:
``int("1_0")`` is 10, ``float("1e-9")`` a nanosecond, ``float("inf")``
endless, ``int("+5")`` and full-width digits pass as well. Each of those set
a configuration nobody intended without any message. The settings of the
API (pool size, timeouts, intervals) are therefore read only as plain
decimal numbers: ASCII digits, for seconds optionally a point and digits.
The callers check the range and name the variable in their error.
"""

from __future__ import annotations

import re

_WHOLE = re.compile(r"[0-9]+")
_DECIMAL = re.compile(r"[0-9]+(\.[0-9]+)?")


def plain_whole_number(raw: str) -> int | None:
    """``raw`` (blanks around it ignored) as a whole number, or ``None``.

    :param raw: the variable's text.
    :returns: the value for ASCII digits only (``"10"``, ``" 4 "``);
        ``None`` for anything else (``"1_0"``, ``"+5"``, ``"2.5"``, ``"1e3"``).
    """

    text = raw.strip()
    return int(text) if _WHOLE.fullmatch(text) else None


def plain_decimal(raw: str) -> float | None:
    """``raw`` (blanks around it ignored) as a decimal number, or ``None``.

    :param raw: the variable's text.
    :returns: the value for digits with an optional point and digits
        (``"7.5"``, ``"30"``); ``None`` for anything else (``"1e-9"``,
        ``"nan"``, ``"inf"``, ``"-1"``, ``".5"``, ``"1_0"``).
    """

    text = raw.strip()
    return float(text) if _DECIMAL.fullmatch(text) else None
