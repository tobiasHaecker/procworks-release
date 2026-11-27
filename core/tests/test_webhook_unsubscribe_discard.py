# SPDX-License-Identifier: BUSL-1.1
"""Deleting a webhook subscription stops every delivery to its old target.

After ``DELETE /v1/webhooks/{id}`` the still open outbox entries of that
subscription were delivered on (one entry: three more attempts until
``DEAD``) -- to a target the operator had just removed, and unsigned, since
the secret came from the deleted subscription.

Now (pinned on the in-memory store and on SQLAlchemy over SQLite):

* deleting the subscription marks its open entries (``PENDING``/``FAILED``)
  ``DISCARDED`` with the reason :data:`procworks.outbox.SUBSCRIPTION_DELETED`
  -- nothing more goes to the old target;
* an entry that is being delivered at that very moment cannot be recalled:
  the one request in flight completes (it is in the delivery log), its
  outcome is not recorded over the discard, and it is never attempted again;
* an entry whose subscription no longer exists when a dispatcher reserves it
  (created by a request that read the subscription just before it was
  deleted, or left over from before) is discarded, not sent;
* entries of other subscriptions, and entries already ``DELIVERED`` or
  ``DEAD``, keep their state.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import procworks.api as api_module
import procworks.outbox as outbox_module
from procworks.db import SqlAlchemyWebhookStore
from procworks.model import OutboxState
from procworks.outbox import SUBSCRIPTION_DELETED, OutboxDispatcher
from procworks.store import InMemoryWebhookStore

_URL = "https://hooks.example.com/procworks"


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _Transport:
    """Records deliveries; ``during`` runs inside the first one (in flight)."""

    def __init__(self, status: int = 500) -> None:
        self.status = status
        self.calls: list[str] = []
        self.during: Any = None

    def post(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
        self.calls.append(url)
        if self.during is not None:
            hook, self.during = self.during, None
            hook()
        return self.status


@pytest.fixture(autouse=True)
def _allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROCWORKS_WEBHOOK_ALLOWLIST", "hooks.example.com")

    def lookup(host: str) -> list[str]:
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            return ["93.184.216.34"]

    monkeypatch.setattr(outbox_module, "_lookup", lookup)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "sqlite":
        return SqlAlchemyWebhookStore(f"sqlite:///{tmp_path / 'hooks.db'}", create_tables=True)
    return InMemoryWebhookStore()


def _setup(store: Any, status: int = 500) -> tuple[OutboxDispatcher, _Transport, _Clock]:
    transport, clock = _Transport(status), _Clock()
    return OutboxDispatcher(store, transport=transport, now=clock), transport, clock


def test_open_entries_are_discarded_and_never_sent_again(store: Any) -> None:
    disp, transport, clock = _setup(store)
    sub = disp.subscribe(_URL, ["task.ready"], "")
    [entry] = disp.emit("task.ready", {"n": 1})
    assert disp.dispatch_pending() == 1  # one failed attempt, back-off pending
    assert store.get_entry(entry.id).state is OutboxState.FAILED

    disp.unsubscribe(sub.id)

    stored = store.get_entry(entry.id)
    assert (stored.state, stored.last_error) == (OutboxState.DISCARDED, SUBSCRIPTION_DELETED)
    clock.t += 3600
    assert disp.dispatch_pending() == 0
    assert transport.calls == [_URL]  # only the attempt before the deletion


def test_an_entry_in_flight_completes_once_and_is_not_retried(store: Any) -> None:
    """The request already on its way cannot be recalled -- but it is the last."""

    disp, transport, clock = _setup(store, status=500)
    sub = disp.subscribe(_URL, ["task.ready"], "")
    [entry] = disp.emit("task.ready", {"n": 1})
    transport.during = lambda: disp.unsubscribe(sub.id)

    disp.dispatch_pending()

    stored = store.get_entry(entry.id)
    assert (stored.state, stored.last_error) == (OutboxState.DISCARDED, SUBSCRIPTION_DELETED)
    assert [d.ok for d in store.list_deliveries()] == [False]  # the attempt is logged
    clock.t += 3600
    disp.dispatch_pending()
    assert transport.calls == [_URL]


def test_an_entry_without_its_subscription_is_discarded_not_sent(store: Any) -> None:
    """A request read the subscription just before it was deleted."""

    disp, transport, _clock = _setup(store)
    sub = disp.subscribe(_URL, ["task.ready"], "")
    store.delete_subscription(sub.id)  # behind the dispatcher's back
    entry = outbox_module._new_entry(
        sub.id, "task.ready", _URL, {"n": 1}, max_attempts=5, now=1000.0
    )
    store.put_entry(entry)

    assert disp.dispatch_pending() == 0

    stored = store.get_entry(entry.id)
    assert (stored.state, stored.last_error) == (OutboxState.DISCARDED, SUBSCRIPTION_DELETED)
    assert stored.attempts == 0  # nothing was attempted
    assert transport.calls == []


def test_other_subscriptions_and_finished_entries_keep_their_state(store: Any) -> None:
    """Negativ: only the open entries of the deleted subscription change."""

    disp, transport, _clock = _setup(store, status=200)
    gone = disp.subscribe(_URL, ["task.ready"], "")
    kept = disp.subscribe(_URL + "/other", ["task.ready"], "")
    delivered = disp.emit("task.ready", {"n": 1})
    disp.dispatch_pending()  # both DELIVERED
    transport.status = 500
    open_entries = disp.emit("task.ready", {"n": 2})

    disp.unsubscribe(gone.id)

    by_sub = {e.subscription_id: e for e in open_entries}
    assert store.get_entry(by_sub[gone.id].id).state is OutboxState.DISCARDED
    assert store.get_entry(by_sub[kept.id].id).state is OutboxState.PENDING
    assert {store.get_entry(e.id).state for e in delivered} == {OutboxState.DELIVERED}


def test_the_api_delete_discards_the_open_entries() -> None:
    client = TestClient(api_module.app)
    created = client.post(
        "/v1/webhooks", json={"url": _URL, "events": ["task.ready"], "secret_ref": ""}
    )
    assert created.status_code in (200, 201), created.text
    sid = created.json()["id"]
    store = api_module._outbox._store
    entry = outbox_module._new_entry(sid, "task.ready", _URL, {}, max_attempts=5, now=0.0)
    entry.next_attempt_at = 10**12  # waiting for its back-off
    store.put_entry(entry)

    assert client.delete(f"/v1/webhooks/{sid}").status_code == 204

    stored = store.get_entry(entry.id)
    assert (stored.state, stored.last_error) == (OutboxState.DISCARDED, SUBSCRIPTION_DELETED)
