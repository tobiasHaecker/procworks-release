# SPDX-License-Identifier: BUSL-1.1
"""The background escalation sweep runs at most once a second.

``PROCWORKS_ESCALATION_SWEEP_SECONDS`` accepted any number greater than 0.
``0.001`` started a near busy loop: about 1000 database transactions a
second and most of a CPU core, without any message. Values below one second
are now refused at start, naming the variable and the bound -- like every
other invalid value; ``0`` (off), unset/empty (60 s) and every value from
1 second up to one day work as before. Only plain decimal numbers count
(``1e3``, ``1_0`` and the like set values nobody meant to set).
"""

from __future__ import annotations

import pytest

from procworks import escalation_timer

_VAR = escalation_timer.INTERVAL_VARIABLE


@pytest.mark.parametrize("raw", ["0.001", "0.5", "0.999", "1e-9", "1e3", "1_0", "+5", "86401"])
def test_values_outside_the_bounds_or_not_plain_abort_with_the_bound_named(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(_VAR, raw)

    with pytest.raises(ValueError, match=_VAR) as refused:
        escalation_timer.sweep_interval()
    assert "0 (off) or a number of seconds from 1 to 86400" in str(refused.value)
    assert repr(raw) in str(refused.value)


@pytest.mark.parametrize(("raw", "seconds"), [("1", 1.0), ("1.5", 1.5), ("60", 60.0),
                                              ("86400", 86400.0), ("0", 0.0), ("0.0", 0.0),
                                              (" 7.5 ", 7.5), ("", 60.0)])
def test_off_the_default_and_values_within_the_bounds_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, raw: str, seconds: float
) -> None:
    monkeypatch.setenv(_VAR, raw)

    assert escalation_timer.sweep_interval() == seconds


def test_the_lower_bound_is_one_second() -> None:
    assert escalation_timer.MIN_INTERVAL_SECONDS == 1.0


def test_the_api_description_of_the_sweep_endpoint_names_the_background_sweep() -> None:
    """The OpenAPI text is customer-facing: no more "sweeps on every read"."""

    from fastapi.testclient import TestClient

    from procworks.api import app

    spec = TestClient(app).get("/openapi.json").json()
    text = spec["paths"]["/admin/escalations/sweep"]["post"]["description"]

    assert "background" in text
    assert _VAR in text
    assert "every read" not in text and "lazy" not in text
