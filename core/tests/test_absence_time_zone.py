# SPDX-License-Identifier: BUSL-1.1
"""An absence window names its time zone, or it is refused.

``2031-02-01T00:00:00`` without an offset was stored as given and silently
read as UTC -- for a user in Germany the absence then started an hour or two
off, and nothing said so. The API now refuses a bound without an offset (422,
``ABS.no-time-zone`` with the field) and keeps the offset it was given; the
web client always sends one. Entries stored before keep being read as UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from procworks import api as api_module
from procworks.assignment import absent_agent_ids
from procworks.model import AbsenceEntry

client = TestClient(api_module.app)


def _agent() -> str:
    """A released schema with a known agent; returns the agent id."""

    sid = client.post("/schemas", json={"name": "Abwesenheit Zone"}).json()["id"]
    client.post(f"/schemas/{sid}/serial-insert", json={"label": "A", "after_node_id": "start"})
    client.post(f"/schemas/{sid}/roles", json={"name": "SB", "role_id": "r-zone"})
    client.post(
        f"/schemas/{sid}/agents",
        json={"name": "Nina", "role_ids": ["r-zone"], "agent_id": "a-zone-nina"},
    )
    return "a-zone-nina"


def _post(agent: str, start: str, end: str) -> Any:
    return client.post(f"/agents/{agent}/absences", json={"start_at": start, "end_at": end})


def _stored(agent: str) -> list[AbsenceEntry]:
    return [e for e in api_module._absence_store.list_entries() if e.agent_id == agent]


@pytest.mark.parametrize(
    ("start", "end", "field"),
    [
        ("2031-02-01T00:00:00", "2031-02-02T00:00:00Z", "start_at"),
        ("2031-02-01T00:00:00Z", "2031-02-02T00:00:00", "end_at"),
        ("2031-02-01T00:00:00", "2031-02-02T00:00:00", "start_at"),
    ],
    ids=["start", "end", "both"],
)
def test_a_bound_without_time_zone_is_refused_naming_the_field(
    start: str, end: str, field: str
) -> None:
    agent = _agent()
    before = len(_stored(agent))

    resp = _post(agent, start, end)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "ABS.no-time-zone"
    assert detail["params"] == {"field": field}
    assert len(_stored(agent)) == before


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2031-02-01T00:00:00Z", "2031-02-02T23:59:59Z"),
        ("2031-02-01T00:00:00+01:00", "2031-02-02T23:59:59+01:00"),
    ],
    ids=["utc", "offset"],
)
def test_a_bound_with_time_zone_is_kept_with_it(start: str, end: str) -> None:
    agent = _agent()
    resp = _post(agent, start, end)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    for key, sent in (("start_at", start), ("end_at", end)):
        returned = datetime.fromisoformat(body[key])
        assert returned.tzinfo is not None
        assert returned == datetime.fromisoformat(sent.replace("Z", "+00:00"))


def test_a_window_across_the_summer_time_switch_is_evaluated_by_instant() -> None:
    """Germany switches on 2031-03-30 at 02:00 CET to 03:00 CEST."""

    entry = AbsenceEntry(
        id="abs-dst",
        agent_id="a-dst",
        start_at=datetime.fromisoformat("2031-03-29T12:00:00+01:00"),  # 11:00 UTC
        end_at=datetime.fromisoformat("2031-03-31T12:00:00+02:00"),  # 10:00 UTC
    )
    inside_night = datetime(2031, 3, 30, 1, 30, tzinfo=UTC)  # 03:30 CEST
    last_second = datetime(2031, 3, 31, 10, 0, tzinfo=UTC)  # 12:00 CEST, inclusive
    after_end = datetime(2031, 3, 31, 10, 0, 1, tzinfo=UTC)
    before_start = datetime(2031, 3, 29, 10, 59, 59, tzinfo=UTC)
    assert absent_agent_ids([entry], inside_night) == {"a-dst"}
    assert absent_agent_ids([entry], last_second) == {"a-dst"}
    assert absent_agent_ids([entry], after_end) == frozenset()
    assert absent_agent_ids([entry], before_start) == frozenset()


def test_an_entry_stored_without_zone_is_still_read_as_utc() -> None:
    legacy = AbsenceEntry(
        id="abs-legacy",
        agent_id="a-legacy",
        start_at=datetime(2031, 2, 1, 0, 0),
        end_at=datetime(2031, 2, 2, 0, 0),
    )
    assert absent_agent_ids([legacy], datetime(2031, 2, 1, 12, tzinfo=UTC)) == {"a-legacy"}
