# SPDX-License-Identifier: BUSL-1.1
"""Pool settings are read as plain numbers within sensible bounds.

``int()``/``float()`` accepted more than anybody writes on purpose:
``PROCWORKS_DB_POOL_SIZE=1_0`` became a pool of 10, ``POOL_TIMEOUT=1e-9`` a
wait of a nanosecond (every request competing for a connection ended with 503
at once), and ``POOL_SIZE=100000`` had no upper bound at all -- far beyond
any PostgreSQL ``max_connections``. Now only plain decimal numbers count,
within: pool size 1-100, overflow 0-100, wait 1-300 seconds. Anything else
aborts the start, naming the variable, the value and the bounds.
"""

from __future__ import annotations

import pytest

from procworks.db import engine_options

_PG_URL = "postgresql+psycopg://process:process@postgres:5432/procworks"


@pytest.fixture(autouse=True)
def _defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("PROCWORKS_DB_POOL_SIZE", "PROCWORKS_DB_MAX_OVERFLOW",
                 "PROCWORKS_DB_POOL_TIMEOUT", "PROCWORKS_DB_STATEMENT_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("name", "value", "bounds"),
    [
        ("PROCWORKS_DB_POOL_SIZE", "1_0", "a whole number from 1 to 100"),
        ("PROCWORKS_DB_POOL_SIZE", "+5", "a whole number from 1 to 100"),
        ("PROCWORKS_DB_POOL_SIZE", "1e1", "a whole number from 1 to 100"),
        ("PROCWORKS_DB_POOL_SIZE", "100000", "a whole number from 1 to 100"),
        ("PROCWORKS_DB_POOL_SIZE", "101", "a whole number from 1 to 100"),
        ("PROCWORKS_DB_MAX_OVERFLOW", "1_0", "a whole number from 0 to 100"),
        ("PROCWORKS_DB_MAX_OVERFLOW", "101", "a whole number from 0 to 100"),
        ("PROCWORKS_DB_POOL_TIMEOUT", "1e-9", "a number of seconds from 1 to 300"),
        ("PROCWORKS_DB_POOL_TIMEOUT", "0.5", "a number of seconds from 1 to 300"),
        ("PROCWORKS_DB_POOL_TIMEOUT", "301", "a number of seconds from 1 to 300"),
        ("PROCWORKS_DB_POOL_TIMEOUT", "3_0", "a number of seconds from 1 to 300"),
    ],
)
def test_unplain_or_out_of_bounds_settings_abort_with_the_bounds_named(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, bounds: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name) as refused:
        engine_options(_PG_URL)
    assert f"expected {bounds}" in str(refused.value)
    assert repr(value) in str(refused.value)


@pytest.mark.parametrize(
    ("size", "overflow", "timeout"),
    [("1", "0", "1"), ("100", "100", "300"), (" 10 ", "10", "7.5"), ("20", "0", "30")],
)
def test_values_within_the_bounds_are_taken(
    monkeypatch: pytest.MonkeyPatch, size: str, overflow: str, timeout: str
) -> None:
    monkeypatch.setenv("PROCWORKS_DB_POOL_SIZE", size)
    monkeypatch.setenv("PROCWORKS_DB_MAX_OVERFLOW", overflow)
    monkeypatch.setenv("PROCWORKS_DB_POOL_TIMEOUT", timeout)

    options = engine_options(_PG_URL)

    assert (options["pool_size"], options["max_overflow"], options["pool_timeout"]) == (
        int(size), int(overflow), float(timeout)
    )
